"""Uses structured outputs (`messages.parse` with a Pydantic schema) rather than
tool-use-as-schema: same guarantee that the response validates, less plumbing, and
no tool-call round trip to unpack.

The prompt's whole job is to stop the model being helpful. An invented advisor or a
paraphrased abstract is worse than an empty field, because the human reviewing the
form has no way to tell a confident guess from a real reading of the document.

Nothing here is a hard dependency of the flow: on any failure the caller falls back
to tier 2 (see pipeline.py). Extraction never blocks a submission.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scholardrop.extract.models import ExtractedMetadata, SourceTier, items, text
from scholardrop.extract.reader import PdfDocument
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")

# Tier 1 asserts a single confidence: the model read the document, or it said
# nothing. Per-field confidence from the model itself would be self-reported and
# not worth much.
LLM_CONFIDENCE = 0.85
MAX_TOKENS = 4096
# Front matter plus tail; enough for any ETD's title page, committee page, and
# abstract without paying for the whole thesis.
MAX_CHARS = 60_000
# The warm-up call only exists to compile the grammar; it needs room for a
# schema-shaped answer of nulls and nothing more.
WARM_MAX_TOKENS = 256


class LlmUnavailable(Exception):
    """No API key, or the API could not be used. Caller falls back to tier 2."""


class LlmExtraction(BaseModel):
    """The schema Claude must fill. Flat and neutral, exactly like the model."""

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


def build_prompt(document: PdfDocument) -> str:
    """The user turn: the document text, plus what little context we have."""
    body = document.text_for_llm[:MAX_CHARS]
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


async def extract(
    document: PdfDocument, settings: Settings, *, client: Any | None = None
) -> ExtractedMetadata:
    """Ask Claude for the metadata. Raises LlmUnavailable for the caller to handle."""
    if not document.has_text:
        raise LlmUnavailable("PDF has no extractable text; nothing to send")

    api_client = client or _default_client(settings)
    response = await _parse_with_retry(api_client, build_prompt(document), settings)

    if getattr(response, "stop_reason", None) == "refusal":
        raise LlmUnavailable("the model declined to process this document")

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise LlmUnavailable("response contained no parsed output")

    log.info(
        "tier 1 extraction ok (model=%s, in=%s, out=%s)",
        getattr(response, "model", settings.anthropic_model),
        getattr(getattr(response, "usage", None), "input_tokens", "?"),
        getattr(getattr(response, "usage", None), "output_tokens", "?"),
    )
    return to_metadata(parsed)


async def warm_grammar(settings: Settings, *, client: Any | None = None) -> None:
    """Compile `LlmExtraction`'s grammar before a real submission needs it.

    The first `parse` call with a given output schema pays to compile its grammar
    server-side. Observed on this schema: ~105s cold and a 400 'Grammar compilation
    timed out' when it does not make it, against ~17s once the grammar is cached.
    A submission should never be the request that discovers this, so a throwaway
    call at startup takes the hit instead. Raises for the caller to log.
    """
    api_client = client or _default_client(settings)
    await api_client.messages.parse(
        model=settings.anthropic_model,
        max_tokens=WARM_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"effort": settings.anthropic_effort},
        messages=[{"role": "user", "content": WARM_PROMPT}],
        output_format=LlmExtraction,
    )


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
        # 401/403 (key), 413 (too big), 422 (schema) -- all fail identically twice.
        return False

    # No HTTP status at all: the request did not complete. Connection resets and
    # timeouts are exactly what a second attempt is for.
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return bool(_RETRYABLE_MESSAGE.search(str(exc)))


async def _parse_with_retry(api_client: Any, prompt: str, settings: Settings) -> Any:
    """Call the API, retrying the failures where that is not pointless."""
    attempts = max(1, settings.extraction_retry_attempts)
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await api_client.messages.parse(
                model=settings.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                output_config={"effort": settings.anthropic_effort},
                messages=[{"role": "user", "content": prompt}],
                output_format=LlmExtraction,
            )
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


def to_metadata(parsed: LlmExtraction) -> ExtractedMetadata:
    """Wrap the flat schema in provenance-carrying fields."""
    tier = SourceTier.LLM
    advisor = (parsed.advisor or "").strip()
    # Rule 4 says never list the advisor twice; enforce it rather than trusting it.
    members = [m for m in parsed.committee_members if m.strip() and m.strip() != advisor]
    return ExtractedMetadata(
        title=text(parsed.title, LLM_CONFIDENCE, tier),
        authors=items(parsed.authors, LLM_CONFIDENCE, tier),
        advisor=text(advisor, LLM_CONFIDENCE, tier),
        committee_members=items(members, LLM_CONFIDENCE, tier),
        keywords=items(parsed.keywords, LLM_CONFIDENCE, tier),
        abstract=text(parsed.abstract, LLM_CONFIDENCE, tier),
        date_issued=text(parsed.date_issued, LLM_CONFIDENCE, tier),
        degree=text(parsed.degree, LLM_CONFIDENCE, tier),
        department=text(parsed.department, LLM_CONFIDENCE, tier),
        language=text(parsed.language, LLM_CONFIDENCE, tier),
        doc_type=text(parsed.doc_type, LLM_CONFIDENCE, tier),
        tier_used=tier,
    )


def _default_client(settings: Settings) -> Any:
    if not settings.anthropic_api_key:
        raise LlmUnavailable("no Anthropic API key configured")
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise LlmUnavailable(f"anthropic SDK unavailable: {exc}") from exc
    return AsyncAnthropic(api_key=settings.anthropic_api_key)
