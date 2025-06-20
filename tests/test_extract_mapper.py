"""Mapping neutral fields onto DSpace metadata, against the shipped profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from scholardrop.config import MappingProfile, load_registry
from scholardrop.extract.mapper import map_metadata
from scholardrop.extract.models import ExtractedMetadata, SourceTier, items, text


@pytest.fixture
def profile(shipped_config_dir: Path) -> MappingProfile:
    registry = load_registry(shipped_config_dir)
    return registry.profile_for(registry.instances["shareok"])


@pytest.fixture
def metadata() -> ExtractedMetadata:
    tier = SourceTier.LLM
    return ExtractedMetadata(
        title=text("A Study of Prairie Streams", 0.9, tier),
        authors=items(["Okonkwo-Bell, Margaret", "Ferrari, Lucia"], 0.9, tier),
        advisor=text("Vasquez, Helen", 0.9, tier),
        committee_members=items(["Adeyemi, Samuel", "Raghunathan, Priya"], 0.9, tier),
        keywords=items(["sediment transport", "Great Plains"], 0.9, tier),
        abstract=text("Ephemeral streams move a disproportionate share of sediment.", 0.9, tier),
        date_issued=text("2019", 0.9, tier),
        degree=text("Doctor of Philosophy", 0.9, tier),
        department=text("School of Geosciences", 0.9, tier),
        language=text("en", 0.9, tier),
        doc_type=text("dissertation", 0.9, tier),
        tier_used=tier,
    )


def test_maps_to_the_profiles_metadata_fields(metadata, profile) -> None:
    mapped = map_metadata(metadata, profile)

    assert mapped.fields["dc.title"] == ["A Study of Prairie Streams"]
    assert mapped.fields["dc.contributor.author"] == ["Okonkwo-Bell, Margaret", "Ferrari, Lucia"]
    assert mapped.fields["dc.contributor.advisor"] == ["Vasquez, Helen"]
    assert mapped.fields["dc.contributor.committeeMember"] == [
        "Adeyemi, Samuel",
        "Raghunathan, Priya",
    ]
    assert mapped.fields["dc.subject"] == ["sediment transport", "Great Plains"]
    assert mapped.fields["dc.date.issued"] == ["2019"]
    assert "Ephemeral streams" in mapped.fields["dc.description.abstract"][0]


def test_author_order_is_preserved(metadata, profile) -> None:
    """Author order is meaningful; alphabetising it would be a data error."""
    mapped = map_metadata(metadata, profile)

    assert mapped.fields["dc.contributor.author"][0] == "Okonkwo-Bell, Margaret"


def test_unmapped_fields_are_reported_not_dropped(metadata, profile) -> None:
    """shareok-etd maps seven of eleven fields; the rest must surface for the user."""
    mapped = map_metadata(metadata, profile)

    assert set(mapped.unmapped) == {"degree", "department", "language", "doc_type"}
    assert mapped.unmapped["degree"] == ["Doctor of Philosophy"]


def test_absent_fields_produce_no_metadata(profile) -> None:
    mapped = map_metadata(ExtractedMetadata(), profile)

    assert mapped.fields == {}
    assert mapped.unmapped == {}


def test_empty_list_is_not_an_empty_value(profile) -> None:
    metadata = ExtractedMetadata(authors=items([]), title=text("Only A Title", 0.9, SourceTier.LLM))

    mapped = map_metadata(metadata, profile)

    assert "dc.contributor.author" not in mapped.fields
    assert mapped.fields["dc.title"] == ["Only A Title"]


def test_two_fields_can_share_one_metadata_field() -> None:
    """A profile may fold advisor and committee into one field; don't clobber."""
    profile = MappingProfile(
        name="folded",
        fields={
            "advisor": "dc.contributor.advisor",
            "committee_members": "dc.contributor.advisor",
        },
    )
    metadata = ExtractedMetadata(
        advisor=text("Vasquez, Helen", 0.9, SourceTier.LLM),
        committee_members=items(["Adeyemi, Samuel"], 0.9, SourceTier.LLM),
    )

    mapped = map_metadata(metadata, profile)

    assert mapped.fields["dc.contributor.advisor"] == ["Vasquez, Helen", "Adeyemi, Samuel"]


def test_duplicate_values_are_collapsed_within_a_field() -> None:
    profile = MappingProfile(
        name="folded",
        fields={"advisor": "dc.contributor.x", "committee_members": "dc.contributor.x"},
    )
    metadata = ExtractedMetadata(
        advisor=text("Vasquez, Helen", 0.9, SourceTier.LLM),
        committee_members=items(["Vasquez, Helen", "Adeyemi, Samuel"], 0.9, SourceTier.LLM),
    )

    mapped = map_metadata(metadata, profile)

    assert mapped.fields["dc.contributor.x"] == ["Vasquez, Helen", "Adeyemi, Samuel"]


def test_low_confidence_is_carried_through_to_the_metadata_field(profile) -> None:
    metadata = ExtractedMetadata(
        title=text("Guessed From Layout", 0.4, SourceTier.HEURISTIC),
        authors=items(["Doe, Jane"], 0.9, SourceTier.LLM),
    )

    mapped = map_metadata(metadata, profile)

    assert mapped.low_confidence["dc.title"] is True
    assert mapped.low_confidence["dc.contributor.author"] is False


def test_patch_value_shape_matches_dspace(metadata, profile) -> None:
    """DSpace wants [{"value": "..."}] per field (DESIGN.md §3)."""
    patch_values = map_metadata(metadata, profile).as_patch_values()

    assert patch_values["dc.title"] == [{"value": "A Study of Prairie Streams"}]
    assert patch_values["dc.contributor.author"] == [
        {"value": "Okonkwo-Bell, Margaret"},
        {"value": "Ferrari, Lucia"},
    ]


def test_a_second_profile_needs_no_code_change(metadata) -> None:
    """The point of the mapping profile: a different repository is config only."""
    other = MappingProfile(
        name="other-repo",
        fields={
            "title": "thesis.title",
            "degree": "thesis.degree.name",
            "authors": "local.creator",
        },
    )

    mapped = map_metadata(metadata, other)

    assert mapped.fields["thesis.title"] == ["A Study of Prairie Streams"]
    assert mapped.fields["thesis.degree.name"] == ["Doctor of Philosophy"]
    assert mapped.fields["local.creator"] == ["Okonkwo-Bell, Margaret", "Ferrari, Lucia"]
    assert "abstract" in mapped.unmapped
