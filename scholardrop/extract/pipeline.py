"""Tier selection and fallback (DESIGN.md §5).

Two rules, and they are not the same rule:

1. **Extraction never blocks a submission.** If tier 1 is unavailable or errors,
   tier 2 runs; if that finds nothing, the user gets an honest empty form with a
   notice. A failure here is never a dead end.
2. **`force_local` is a promise about where the bytes go.** A submission the user
   marked as restricted is extracted by a model on this box or by nothing at all.
   It never falls back to the cloud -- not when the local server is down, not
   when the instance is configured for cloud, not ever. The fallback for a
   restricted document is heuristics, which never leave the process.

Reading and parsing a PDF is CPU-bound, and ScholarDrop is one process with one
event loop (DESIGN.md §2). Run on the loop, parsing a large scanned thesis stalls
every other user's request for as long as it takes -- so both synchronous stages,
`read_pdf` and the tier-2 heuristics, go to a worker thread. Tier 1 is already
network-bound and awaits properly.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from scholardrop.config import Instance
from scholardrop.extract import heuristic, llm
from scholardrop.extract.backends import LLMBackend, LlmUnavailable, select_backend
from scholardrop.extract.language import detect_language
from scholardrop.extract.models import ExtractedMetadata, SourceTier, text
from scholardrop.extract.reader import PdfDocument, read_pdf
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")


async def extract_from_pdf(
    source: Path | str | bytes,
    *,
    instance: Instance,
    settings: Settings,
    client: object | None = None,
    force_local: bool = False,
) -> tuple[ExtractedMetadata, PdfDocument]:
    """Read a PDF and extract its metadata with the instance's configured tier."""
    document = await asyncio.to_thread(read_pdf, source)
    metadata = await extract_from_document(
        document,
        instance=instance,
        settings=settings,
        client=client,
        force_local=force_local,
    )
    return metadata, document


async def extract_from_document(
    document: PdfDocument,
    *,
    instance: Instance,
    settings: Settings,
    client: object | None = None,
    force_local: bool = False,
) -> ExtractedMetadata:
    metadata = await _extract(
        document, instance=instance, settings=settings, client=client, force_local=force_local
    )
    return _with_language(metadata, document, settings)


def _with_language(
    metadata: ExtractedMetadata, document: PdfDocument, settings: Settings
) -> ExtractedMetadata:
    """Fill in the document's language when nothing else did (DESIGN.md §13.4).

    Only ever fills a gap: a model that read the language off the document, and a
    registry record that carries it, both know more than a word count does. The
    value is tier 2 and flagged as such -- a detected language is a guess, and one
    the person depositing can correct in a dropdown.
    """
    if not settings.bilingual_enabled or metadata.language.present:
        return metadata
    code, confidence = detect_language(document.text_for_llm)
    if not code:
        return metadata
    log.info("detected document language %s", code)
    return metadata.model_copy(update={"language": text(code, confidence, SourceTier.HEURISTIC)})


async def _extract(
    document: PdfDocument,
    *,
    instance: Instance,
    settings: Settings,
    client: object | None = None,
    force_local: bool = False,
) -> ExtractedMetadata:
    if instance.extraction == "heuristic":
        # Configured off, not failed -- e.g. an instance whose documents must not
        # be sent anywhere at all (DESIGN.md §5). `force_local` only ever narrows
        # where a document may go, so it cannot switch a model back on here.
        log.info("instance %s is configured for heuristic extraction", instance.key)
        return await _heuristic(document)

    try:
        backend: LLMBackend = select_backend(
            settings,
            mode=instance.extraction,
            force_local=force_local,
            client=client,
        )
    except LlmUnavailable as exc:
        # Only reachable via force_local with no local model configured: the
        # restricted document stays here and gets heuristics.
        log.info("no local backend for a restricted submission (%s); using heuristics", exc)
        return await _heuristic(document, fallback_reason=str(exc))

    log.info(
        "tier 1 for instance %s via %s%s",
        instance.key,
        backend.label,
        " (restricted: local only)" if force_local else "",
    )
    try:
        return await llm.extract(document, settings, backend=backend)
    except LlmUnavailable as exc:
        log.info("tier 1 unavailable (%s); falling back to heuristics", exc)
        return await _heuristic(document, fallback_reason=str(exc))


async def _heuristic(
    document: PdfDocument, fallback_reason: str | None = None
) -> ExtractedMetadata:
    metadata = await asyncio.to_thread(heuristic.extract, document)
    if metadata.is_empty():
        # An honest nothing. The review form says extraction found nothing rather
        # than pretending the form is simply blank.
        log.info("heuristic extraction found no metadata")
        return metadata.model_copy(
            update={"tier_used": SourceTier.NONE, "fallback_reason": fallback_reason}
        )
    return metadata.model_copy(update={"fallback_reason": fallback_reason})
