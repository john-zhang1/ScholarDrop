"""ExtractedMetadata -> DSpace metadata fields (DESIGN.md §5).

This is the only place the two vocabularies meet. The extractor stays ignorant of
Dublin Core; the mapping profile owns every repository-specific field name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scholardrop.config import MappingProfile
from scholardrop.extract.models import ExtractedMetadata


@dataclass
class MappedMetadata:
    """DSpace-shaped values, plus what could not be placed."""

    fields: dict[str, list[str]] = field(default_factory=dict)
    # Extracted values with no entry in the profile. The review form shows these in a
    # "not placed" list the user can copy from, rather than dropping them silently.
    unmapped: dict[str, list[str]] = field(default_factory=dict)
    # metadata field -> True when any contributing value was low confidence, so the
    # form can flag it for attention.
    low_confidence: dict[str, bool] = field(default_factory=dict)

    def values(self, metadata_field: str) -> list[str]:
        return self.fields.get(metadata_field, [])

    def as_patch_values(self) -> dict[str, list[dict[str, str]]]:
        """Shape DSpace's JSON Patch wants: [{"value": "..."}, ...]."""
        return {name: [{"value": v} for v in values] for name, values in self.fields.items()}


def map_metadata(metadata: ExtractedMetadata, profile: MappingProfile) -> MappedMetadata:
    """Apply a mapping profile, keeping order and merging fields that share a target."""
    result = MappedMetadata()

    for name in metadata.field_names():
        extracted = metadata.field(name)
        if not extracted.present:
            continue

        values = extracted.value if isinstance(extracted.value, list) else [str(extracted.value)]
        values = [v for v in values if v]
        if not values:
            continue

        target = profile.metadata_field(name)
        if target is None:
            result.unmapped[name] = values
            continue

        # Two neutral fields can legitimately map to one metadata field (e.g. advisor
        # and committee_members both to dc.contributor.advisor). Append, don't clobber.
        existing = result.fields.setdefault(target, [])
        for value in values:
            if value not in existing:
                existing.append(value)
        result.low_confidence[target] = result.low_confidence.get(target, False) or (
            extracted.low_confidence
        )

    return result
