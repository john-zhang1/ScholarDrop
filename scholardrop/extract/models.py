"""What extraction produces (DESIGN.md §5).

Every field carries its value *plus* where it came from and how much to trust it,
so the review UI can flag low-confidence guesses for human attention instead of
presenting a heuristic's best effort as fact.

Field names here are deliberately neutral -- no Dublin Core. `Mapper` plus the
instance's mapping profile turns them into metadata fields, which is what lets a
second repository with different field names be a config change.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Below this, the review form should visibly flag the value (Phase 4).
LOW_CONFIDENCE = 0.6


class SourceTier(StrEnum):
    """Where a value came from. Ordered loosely by trustworthiness."""

    LLM = "llm"  # tier 1: structured extraction from document text
    XMP = "xmp"  # embedded PDF metadata -- authored, not inferred
    HEURISTIC = "heuristic"  # tier 2: layout and pattern matching
    NONE = "none"  # nothing found


class Extracted[T](BaseModel):
    """One field's value with its provenance."""

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    confidence: float = 0.0
    source_tier: SourceTier = SourceTier.NONE

    @property
    def present(self) -> bool:
        if self.value is None:
            return False
        if isinstance(self.value, str | list):
            return len(self.value) > 0
        return True

    @property
    def low_confidence(self) -> bool:
        """True when a human should look at this before it is deposited."""
        return self.present and self.confidence < LOW_CONFIDENCE


def text(
    value: str | None,
    confidence: float = 0.0,
    tier: SourceTier = SourceTier.NONE,
) -> Extracted[str]:
    cleaned = (value or "").strip()
    if not cleaned:
        return Extracted[str]()
    return Extracted[str](value=cleaned, confidence=confidence, source_tier=tier)


def items(
    values: list[str] | None,
    confidence: float = 0.0,
    tier: SourceTier = SourceTier.NONE,
) -> Extracted[list[str]]:
    cleaned = [v.strip() for v in (values or []) if v and v.strip()]
    # Order matters (author order is meaningful), so dedupe without sorting.
    seen: set[str] = set()
    unique = [v for v in cleaned if not (v in seen or seen.add(v))]
    if not unique:
        return Extracted[list[str]](value=[])
    return Extracted[list[str]](value=unique, confidence=confidence, source_tier=tier)


class ExtractedMetadata(BaseModel):
    """The neutral result of extraction.

    Field names must match `EXTRACTED_FIELDS` in scholardrop/config.py -- mapping
    profiles are validated against that set, and a test asserts the two agree.
    """

    title: Extracted[str] = Field(default_factory=Extracted[str])
    authors: Extracted[list[str]] = Field(default_factory=Extracted[list[str]])
    advisor: Extracted[str] = Field(default_factory=Extracted[str])
    committee_members: Extracted[list[str]] = Field(default_factory=Extracted[list[str]])
    keywords: Extracted[list[str]] = Field(default_factory=Extracted[list[str]])
    abstract: Extracted[str] = Field(default_factory=Extracted[str])
    date_issued: Extracted[str] = Field(default_factory=Extracted[str])
    degree: Extracted[str] = Field(default_factory=Extracted[str])
    department: Extracted[str] = Field(default_factory=Extracted[str])
    language: Extracted[str] = Field(default_factory=Extracted[str])
    doc_type: Extracted[str] = Field(default_factory=Extracted[str])

    # Set by the pipeline: which tier actually ran, and why if it fell back.
    tier_used: SourceTier = SourceTier.NONE
    fallback_reason: str | None = None

    @classmethod
    def field_names(cls) -> list[str]:
        """The extracted-field names, excluding pipeline bookkeeping."""
        return [
            name
            for name, field in cls.model_fields.items()
            if name not in {"tier_used", "fallback_reason"}
        ]

    def field(self, name: str) -> Extracted[Any]:
        return getattr(self, name)

    def present_fields(self) -> dict[str, Extracted[Any]]:
        return {name: f for name in self.field_names() if (f := self.field(name)).present}

    def low_confidence_fields(self) -> list[str]:
        return [name for name in self.field_names() if self.field(name).low_confidence]

    def is_empty(self) -> bool:
        """True when extraction found nothing -- an honest outcome, not an error."""
        return not self.present_fields()
