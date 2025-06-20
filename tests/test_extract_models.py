from __future__ import annotations

from scholardrop.config import EXTRACTED_FIELDS
from scholardrop.extract.models import (
    LOW_CONFIDENCE,
    ExtractedMetadata,
    SourceTier,
    items,
    text,
)


def test_model_fields_match_the_config_whitelist() -> None:
    """Mapping profiles are validated against EXTRACTED_FIELDS, so the two must agree.

    If this fails, a profile could name a field the extractor never produces (or the
    other way round) and nobody would notice until a real submission.
    """
    assert set(ExtractedMetadata.field_names()) == set(EXTRACTED_FIELDS)


def test_pipeline_bookkeeping_is_not_an_extracted_field() -> None:
    assert "tier_used" not in ExtractedMetadata.field_names()
    assert "fallback_reason" not in ExtractedMetadata.field_names()


def test_absent_by_default() -> None:
    metadata = ExtractedMetadata()

    assert metadata.is_empty()
    assert metadata.title.present is False
    assert metadata.title.source_tier is SourceTier.NONE
    assert metadata.low_confidence_fields() == []


def test_text_strips_and_treats_blank_as_absent() -> None:
    assert text("  A Title  ", 0.9, SourceTier.LLM).value == "A Title"
    assert text("   ").present is False
    assert text(None).present is False


def test_items_strips_drops_blanks_and_dedupes_without_reordering() -> None:
    result = items(["  Doe, Jane ", "", "Roe, Richard", "Doe, Jane"], 0.9, SourceTier.LLM)

    assert result.value == ["Doe, Jane", "Roe, Richard"]


def test_empty_list_is_present_false() -> None:
    result = items([])

    assert result.value == []
    assert result.present is False


def test_low_confidence_flags_only_present_values() -> None:
    assert text("Guess", LOW_CONFIDENCE - 0.1, SourceTier.HEURISTIC).low_confidence is True
    assert text("Solid", LOW_CONFIDENCE + 0.1, SourceTier.LLM).low_confidence is False
    # An absent field is not "low confidence" -- there is nothing to review.
    assert text(None).low_confidence is False


def test_present_and_low_confidence_summaries() -> None:
    metadata = ExtractedMetadata(
        title=text("Solid Title", 0.9, SourceTier.LLM),
        abstract=text("A guessed abstract of sufficient length.", 0.4, SourceTier.HEURISTIC),
    )

    assert sorted(metadata.present_fields()) == ["abstract", "title"]
    assert metadata.low_confidence_fields() == ["abstract"]
    assert metadata.is_empty() is False


def test_fields_are_immutable() -> None:
    """Extracted values are evidence; mutating them in place would lose provenance."""
    import pydantic
    import pytest

    field = text("A Title", 0.9, SourceTier.LLM)

    with pytest.raises(pydantic.ValidationError):
        field.value = "Something Else"  # type: ignore[misc]
