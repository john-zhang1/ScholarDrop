"""Metadata extraction from PDFs (DESIGN.md §5)."""

from scholardrop.extract.mapper import MappedMetadata, map_metadata
from scholardrop.extract.models import (
    LOW_CONFIDENCE,
    Extracted,
    ExtractedMetadata,
    SourceTier,
)
from scholardrop.extract.pipeline import extract_from_document, extract_from_pdf
from scholardrop.extract.reader import PdfDocument, PdfUnreadable, looks_like_pdf, read_pdf

__all__ = [
    "LOW_CONFIDENCE",
    "Extracted",
    "ExtractedMetadata",
    "MappedMetadata",
    "PdfDocument",
    "PdfUnreadable",
    "SourceTier",
    "extract_from_document",
    "extract_from_pdf",
    "looks_like_pdf",
    "map_metadata",
    "read_pdf",
]
