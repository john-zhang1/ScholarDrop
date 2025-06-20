"""Tier selection and fallback (DESIGN.md §5).

The rule that matters: **extraction never blocks a submission**. If tier 1 is
unavailable or errors, tier 2 runs; if that finds nothing, the user gets an honest
empty form with a notice. A failure here is never a dead end.
"""

from __future__ import annotations

import logging
from pathlib import Path

from scholardrop.config import Instance
from scholardrop.extract import heuristic, llm
from scholardrop.extract.models import ExtractedMetadata, SourceTier
from scholardrop.extract.reader import PdfDocument, read_pdf
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")


async def extract_from_pdf(
    source: Path | str | bytes,
    *,
    instance: Instance,
    settings: Settings,
    client: object | None = None,
) -> tuple[ExtractedMetadata, PdfDocument]:
    """Read a PDF and extract its metadata with the instance's configured tier."""
    document = read_pdf(source)
    metadata = await extract_from_document(
        document, instance=instance, settings=settings, client=client
    )
    return metadata, document


async def extract_from_document(
    document: PdfDocument,
    *,
    instance: Instance,
    settings: Settings,
    client: object | None = None,
) -> ExtractedMetadata:
    if instance.extraction == "heuristic":
        # Configured off, not failed -- e.g. an instance whose documents must not be
        # sent to a third party (DESIGN.md §5, privacy note).
        log.info("instance %s is configured for heuristic extraction", instance.key)
        return _heuristic(document)

    try:
        return await llm.extract(document, settings, client=client)
    except llm.LlmUnavailable as exc:
        log.info("tier 1 unavailable (%s); falling back to heuristics", exc)
        return _heuristic(document, fallback_reason=str(exc))


def _heuristic(document: PdfDocument, fallback_reason: str | None = None) -> ExtractedMetadata:
    metadata = heuristic.extract(document)
    if metadata.is_empty():
        # An honest nothing. The review form says extraction found nothing rather
        # than pretending the form is simply blank.
        log.info("heuristic extraction found no metadata")
        return metadata.model_copy(
            update={"tier_used": SourceTier.NONE, "fallback_reason": fallback_reason}
        )
    return metadata.model_copy(update={"fallback_reason": fallback_reason})
