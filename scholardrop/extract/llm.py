"""Tier 1 extraction: a model with a forced output schema (DESIGN.md §5).

This module owns the *task*: the schema, the prompts, the input budget, the retry
policy, and the translation into `ExtractedMetadata`. Where the model runs is
`backends.py`'s business -- cloud (Claude) or a local open-weight model for
embargoed documents. The two paths differ only in the backend object; every rule
below applies identically to both.

Structured outputs, not free text: the response is guaranteed to validate against
`LlmExtraction`, server-side, by constrained decoding. That guarantee is what lets
a 4B local model stand in for Claude at all -- what is left to get wrong is a
field's *content*, which the confidence flags and the review step handle.

The prompt's whole job is to stop the model being helpful. An invented advisor or
a paraphrased abstract is worse than an empty field, because the human reviewing
the form has no way to tell a confident guess from a real reading of the document.

Nothing here is a hard dependency of the flow: on any failure the caller falls back
to tier 2 (see pipeline.py). Extraction never blocks a submission.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scholardrop.extract.backends import (
    MAX_TOKENS,
    WARM_MAX_TOKENS,
    AnthropicBackend,
    LLMBackend,
    LlmUnavailable,
    local_backend,
)
from scholardrop.extract.models import ExtractedMetadata, SourceTier, items, text
from scholardrop.extract.reader import PdfDocument
from scholardrop.identifiers.ids import IdentifierKind, UnknownIdentifier, parse_identifier
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")

__all__ = [
    "MAX_TOKENS",
    "WARM_MAX_TOKENS",
    "LlmExtraction",
    "LlmUnavailable",
    "extract",
    "to_metadata",
    "warm_grammar",
]

# Tier 1 asserts a single confidence: the model read the document, or it said
# nothing. Per-field confidence from the model itself would be self-reported and
# not worth much.
LLM_CONFIDENCE = 0.85
# A local 4B model is a weaker reader than Claude, and the review form's job is to
# tell a librarian where to look. Still above LOW_CONFIDENCE (0.6) -- it read the
# document -- but visibly not the same claim.
LOCAL_LLM_CONFIDENCE = 0.7
# Front matter plus tail; enough for any ETD's title page, committee page, and
# abstract without paying for the whole thesis.
MAX_CHARS = 60_000


class LlmExtraction(BaseModel):
    """The schema the model must fill. Flat and neutral, exactly like the model."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, description="Full title of the work, verbatim.")
    authors: list[str] = Field(
        default_factory=list,
        description=(
            "Author name(s). Preserve the source's order and format: if the document "
            "writes 'Doe, Jane' keep that; if it writes 'Jane Doe' keep that."
        ),
    )
    advisor: str | None = Field(
        default=None,
        description=(
            "The supervising advisor, committee chair, or director of the work, if the "
            "document names one as such."
        ),
    )
    committee_members: list[str] = Field(
        default_factory=list,
        description=(
            "Other committee members, excluding the advisor/chair already reported. "
            "Only for documents that list a committee."
        ),
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Author-supplied keywords or subject terms, only if the document lists them.",
    )
    abstract: str | None = Field(
        default=None,
        description=(
            "The abstract, copied verbatim from the document. Do not summarise, "
            "shorten, or rewrite it. Null if the document has no abstract."
        ),
    )
    date_issued: str | None = Field(
        default=None,
        description="Publication or degree date as printed, ideally YYYY or YYYY-MM-DD.",
    )
    degree: str | None = Field(
        default=None, description="Degree awarded, e.g. 'Doctor of Philosophy'."
    )
    department: str | None = Field(
        default=None, description="Awarding department, school, or program."
    )
    language: str | None = Field(
        default=None, description="Language of the work as an ISO 639-1 code, e.g. 'en'."
    )
    doc_type: str | None = Field(
        default=None,
        description=(
            "Kind of work: 'thesis', 'dissertation', 'article', 'report', 'book chapter', "
            "or similar, if determinable."
        ),
    )
    publisher: str | None = Field(
        default=None,
        description=(
            "Publisher or issuing body, if the document names one. For a thesis this is "
            "usually the university."
        ),
    )
    journal: str | None = Field(
        default=None,
        description=(
            "Journal, proceedings or book the work appeared in, if the document says so. "
            "Null for a thesis or a standalone report."
        ),
    )
    doi: str | None = Field(
        default=None,
        description=(
            "The DOI printed in the document, as '10.xxxx/suffix' with no 'doi:' prefix "
            "or URL around it. Only a DOI that appears in the text -- never construct or "
            "complete one."
        ),
    )


SYSTEM_PROMPT = """\
You extract bibliographic metadata from scholarly documents for a university \
repository. A librarian reviews every field you return before anything is deposited.

Rules, in order of importance:

1. Extract only what is present in the text. Never infer, guess, complete, or \
correct. If the document does not state something, return null (or an empty list). \
An empty field is correct and expected; an invented one is a defect, because the \
reviewer cannot tell your guess from a real reading.
2. Copy the abstract verbatim -- the same words, in the same order, with the same \
punctuation. Do not summarise, trim, translate, or tidy it. If there is no abstract, \
return null rather than writing one.
3. Preserve names exactly as written, including the order the document uses. If the \
document writes "Doe, Jane", return "Doe, Jane"; if it writes "Jane Doe", return \
"Jane Doe". Do not reformat, reorder, or expand initials. Drop honorifics and \
trailing roles ("Dr.", ", Chair") from the name itself.
4. The advisor, chair, or director of the work goes in `advisor`; every other \
committee member goes in `committee_members`. Never list the same person twice. \
Most articles have neither -- return null and an empty list.
5. The text you are given is the front matter and tail of the document, extracted \
from a PDF, so it may be noisy, out of order, or cut mid-sentence. Ignore running \
heads, page numbers, and boilerplate. If the text is unusable, return nulls and \
empty lists rather than reaching.
"""

# The small-model prompt. Same rules, a third of the words: with the schema
# enforced by constrained decoding there is nothing to say about the output shape,
# and a 4B model follows six short imperatives better than five paragraphs about
# why they matter. Every rule that survives is one a gate run caught it breaking.
COMPACT_SYSTEM_PROMPT = """\
Extract bibliographic metadata from this document for a library catalogue.

- Copy from the text. Never guess, infer, or complete. Use null or [] for anything \
the document does not state.
- Copy the abstract word for word. Never summarise or shorten it. No abstract in \
the text means null.
- Copy names as printed, in the printed order ("Doe, Jane" stays "Doe, Jane"). \
Drop "Dr." and trailing roles like ", Chair".
- Advisor, chair, or director -> advisor. Everyone else on the committee -> \
committee_members. Never the same person in both.
- The text is front matter and the tail of a PDF: noisy, possibly out of order. \
Ignore page numbers and running heads.
- Unusable text means nulls and empty lists, not a best effort.
"""


def build_prompt(document: PdfDocument, *, max_chars: int = MAX_CHARS) -> str:
    """The user turn: the document text, plus what little context we have."""
    body = _budgeted_text(document, max_chars)
    facts = [f"page count: {document.page_count}"]
    if title := document.docinfo.get("title"):
        facts.append(f"PDF Title field: {title!r} (may be wrong or a filename)")
    if author := document.docinfo.get("author"):
        facts.append(f"PDF Author field: {author!r} (may be wrong)")
    return (
        "Extract the metadata for this document.\n\n"
        "Document facts:\n" + "\n".join(f"- {fact}" for fact in facts) + "\n\n"
        "Text extracted from the PDF follows, between markers.\n\n"
        f"<document_text>\n{body}\n</document_text>"
    )


_ABSTRACT_HEADING = re.compile(r"^[ \t]*abstract\b[:.]?[ \t]*$", re.IGNORECASE | re.MULTILINE)
# What a small context has to keep, in the order it earns its space: the title
# page, the abstract, and the signature page in the tail.
_TITLE_PAGE_SHARE = 0.45
_TAIL_SHARE = 0.25


def _budgeted_text(document: PdfDocument, max_chars: int) -> str:
    """Fit the document to a context window without losing the three useful parts.

    Truncating `text_for_llm` head-first is fine at 60k chars and wrong at 8k: the
    committee page of an ETD is in the *tail*, and the abstract is often past page
    3. So spend the budget deliberately -- title page, abstract block, signature
    tail -- rather than on whichever pages happened to come first.
    """
    whole = document.text_for_llm
    if len(whole) <= max_chars:
        return whole

    front, back = document.front_text, document.back_text
    tail_budget = int(max_chars * _TAIL_SHARE) if back.strip() else 0
    front_budget = max_chars - tail_budget

    head_budget = (
        int(front_budget * _TITLE_PAGE_SHARE) if len(front) > front_budget else front_budget
    )
    head = front[:head_budget]

    abstract = ""
    if (match := _ABSTRACT_HEADING.search(front, head_budget)) is not None:
        abstract = front[match.start() : match.start() + (front_budget - head_budget)]

    parts = [head]
    if abstract:
        parts.append(f"[... skipped ...]\n\n{abstract}")
    if tail_budget:
        parts.append(f"[... end of document ...]\n\n{back[-tail_budget:]}")
    return "\n\n".join(parts)


async def extract(
    document: PdfDocument,
    settings: Settings,
    *,
    client: Any | None = None,
    backend: LLMBackend | None = None,
) -> ExtractedMetadata:
    """Ask the model for the metadata. Raises LlmUnavailable for the caller to handle.

    `backend` selects where it runs (default: Claude). `client` is the Anthropic
    SDK seam, kept for callers and tests that only ever meant the cloud path.
    """
    if not document.has_text:
        raise LlmUnavailable("PDF has no extractable text; nothing to send")

    backend = backend or AnthropicBackend(settings, client=client)
    is_local = getattr(backend, "local", False)
    prompt = build_prompt(
        document, max_chars=settings.local_llm_max_chars if is_local else MAX_CHARS
    )
    system = COMPACT_SYSTEM_PROMPT if is_local else SYSTEM_PROMPT

    raw = await _extract_with_retry(backend, system, prompt, settings)
    try:
        parsed = LlmExtraction.model_validate(raw)
    except ValidationError as exc:
        # Constrained decoding is supposed to make this unreachable; a server
        # ignoring the schema is a configuration failure, not a bad document.
        raise LlmUnavailable(f"{backend.label} returned output off-schema: {_brief(exc)}") from exc

    return to_metadata(parsed, confidence=LOCAL_LLM_CONFIDENCE if is_local else LLM_CONFIDENCE)


async def warm(backend: LLMBackend) -> None:
    """Pay a backend's first-call cost now rather than during a submission."""
    await backend.warm()


async def warm_grammar(settings: Settings, *, client: Any | None = None) -> None:
    """Compile the cloud backend's grammar at startup (see AnthropicBackend.warm)."""
    await AnthropicBackend(settings, client=client).warm()


async def warm_local(settings: Settings) -> None:
    """Load the local model into RAM at startup, if one is configured."""
    await local_backend(settings).warm()


WARM_PROMPT = (
    "Extract the metadata for this document.\n\n"
    "Text extracted from the PDF follows, between markers.\n\n"
    "<document_text>\n</document_text>"
)

# A 400 is normally a permanent "this request is wrong", with one exception worth
# knowing by name: the grammar compile that ran out of time. Retrying it works,
# because the compile that timed out is still cached by the time we ask again.
_RETRYABLE_MESSAGE = re.compile(
    r"grammar compilation timed out|overloaded|timed? ?out|temporarily unavailable",
    re.IGNORECASE,
)


def _is_retryable(exc: Exception) -> bool:
    """Is asking again the sort of thing that might work?

    Deliberately conservative: an unrecognised failure is treated as permanent, so
    a genuine bad-request bug costs one call rather than several and the user is
    not left waiting on retries that were never going to help.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)

    if isinstance(status, int):
        if status in {408, 409, 429} or status >= 500:
            return True
        if status == 400:
            return bool(_RETRYABLE_MESSAGE.search(str(exc)))
        # 401/403 (key), 404 (model not pulled), 413 (too big), 422 (schema) --
        # all fail identically twice.
        return False

    # No HTTP status at all: the request did not complete. Connection resets and
    # timeouts are exactly what a second attempt is for.
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return bool(_RETRYABLE_MESSAGE.search(str(exc)))


async def _extract_with_retry(
    backend: LLMBackend, system: str, prompt: str, settings: Settings
) -> dict[str, Any]:
    """Call the backend, retrying the failures where that is not pointless."""
    attempts = max(1, settings.extraction_retry_attempts)
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await backend.extract(system=system, prompt=prompt, schema=LlmExtraction)
        except LlmUnavailable:
            # The backend already decided this is not worth asking twice (no key,
            # nothing listening, a refusal). Its message is the honest one.
            raise
        except Exception as exc:  # network, auth, rate limit -- classified below
            last = exc
            if attempt >= attempts or not _is_retryable(exc):
                break
            delay = settings.extraction_retry_backoff_seconds * attempt
            log.info(
                "tier 1 attempt %d/%d failed (%s: %s); retrying in %.1fs",
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    raise LlmUnavailable(f"extraction request failed: {type(last).__name__}: {last}") from last


def to_metadata(parsed: LlmExtraction, *, confidence: float = LLM_CONFIDENCE) -> ExtractedMetadata:
    """Wrap the flat schema in provenance-carrying fields."""
    tier = SourceTier.LLM
    advisor = (parsed.advisor or "").strip()
    # Rule 4 says never list the advisor twice; enforce it rather than trusting it.
    members = [m for m in parsed.committee_members if m.strip() and m.strip() != advisor]
    return ExtractedMetadata(
        title=text(parsed.title, confidence, tier),
        authors=items(parsed.authors, confidence, tier),
        advisor=text(advisor, confidence, tier),
        committee_members=items(members, confidence, tier),
        keywords=items(parsed.keywords, confidence, tier),
        abstract=text(parsed.abstract, confidence, tier),
        date_issued=text(parsed.date_issued, confidence, tier),
        degree=text(parsed.degree, confidence, tier),
        department=text(parsed.department, confidence, tier),
        language=text(parsed.language, confidence, tier),
        doc_type=text(parsed.doc_type, confidence, tier),
        publisher=text(parsed.publisher, confidence, tier),
        journal=text(parsed.journal, confidence, tier),
        # A DOI read off a page points at a *different work* when it is wrong, so
        # it is normalised through the same parser the identifier box uses and
        # dropped when it does not parse -- a malformed DOI is no DOI.
        doi=text(_clean_doi(parsed.doi), confidence, tier),
        tier_used=tier,
    )


def _clean_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        identifier = parse_identifier(raw)
    except UnknownIdentifier:
        log.info("tier 1 returned something that is not a DOI; dropping it")
        return None
    return identifier.value if identifier.kind is IdentifierKind.DOI else None


def _brief(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in exc.errors()[:3]
    )
