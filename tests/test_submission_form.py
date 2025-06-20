"""The review form is built from DSpace's configuration, not from ours."""

from __future__ import annotations

import pytest

from scholardrop.config import MappingProfile
from scholardrop.extract.mapper import map_metadata
from scholardrop.extract.models import ExtractedMetadata, SourceTier, items, text
from scholardrop.submission.form import (
    build_form,
    heading_for,
    message_for,
    parse_fields,
)
from tests.fakes import (
    DEFINITION_SECTIONS,
    FORM_PAGE_ONE,
    FORM_PAGE_TWO,
    SUBMISSION_FORMS,
    make_workspaceitem,
    metadata_values,
)

PROFILE = MappingProfile(
    name="shareok-etd",
    fields={
        "title": "dc.title",
        "authors": "dc.contributor.author",
        "advisor": "dc.contributor.advisor",
        "committee_members": "dc.contributor.committeeMember",
        "keywords": "dc.subject",
        "abstract": "dc.description.abstract",
        "date_issued": "dc.date.issued",
    },
)


def mapped(**fields):
    return map_metadata(ExtractedMetadata(**fields), PROFILE)


def form_for(item=None, **kwargs):
    kwargs.setdefault("sections", DEFINITION_SECTIONS)
    return build_form(item or make_workspaceitem(), SUBMISSION_FORMS, **kwargs)


# ---- reading the field configuration ---------------------------------------


def test_fields_come_from_the_configuration_in_order() -> None:
    fields = parse_fields(FORM_PAGE_ONE)

    assert [f.metadata_field for f in fields][:4] == [
        "dc.contributor.author",
        "dc.title",
        "dc.date.issued",
        "dc.type",
    ]


def test_flags_and_input_types_are_carried_through() -> None:
    by_field = {f.metadata_field: f for f in parse_fields(FORM_PAGE_ONE)}

    assert by_field["dc.title"].mandatory
    assert not by_field["dc.title"].repeatable
    assert by_field["dc.contributor.author"].repeatable
    assert by_field["dc.date.issued"].input_type == "date"
    assert by_field["dc.type"].vocabulary == "common_types"
    assert by_field["dc.type"].is_choice
    assert parse_fields(FORM_PAGE_TWO)[1].multiline  # Abstract is a textarea


def test_extra_selectable_metadata_become_secondary_fields() -> None:
    """DSpace's Identifiers row offers several metadata fields; each is an input,
    but only the first is shown up front."""
    fields = {f.metadata_field: f for f in parse_fields(FORM_PAGE_ONE)}

    assert not fields["dc.identifier.issn"].secondary
    assert fields["dc.identifier.other"].secondary
    assert fields["dc.identifier.other"].label.endswith("Other")


def test_a_section_with_no_configuration_yields_no_fields() -> None:
    assert parse_fields({}) == []


# ---- section structure ------------------------------------------------------


def test_sections_follow_the_submission_definition() -> None:
    form = form_for()

    assert [s.id for s in form.sections] == [
        "collection",
        "traditionalpageone",
        "traditionalpagetwo",
        "upload",
        "license",
    ]
    assert [s.id for s in form.editable_sections] == [
        "traditionalpageone",
        "traditionalpagetwo",
    ]


def test_section_headings_are_english_not_i18n_keys() -> None:
    form = form_for()

    assert form.section("traditionalpageone").heading == "Describe your submission"
    assert heading_for("submit.progressbar.upload", "upload") == "File"
    # An unknown key still reads as something, rather than leaking a dotted key.
    assert heading_for("submit.progressbar.somethingNew", "x") == "Somethingnew"


def test_stored_values_are_loaded_into_their_fields() -> None:
    item = make_workspaceitem(
        sections={"traditionalpageone": {"dc.title": metadata_values("A Stored Title")}}
    )

    field = form_for(item).field("traditionalpageone", "dc.title")

    assert [v.value for v in field.values] == ["A Stored Title"]
    assert not field.prefilled


def test_the_upload_section_reports_its_files() -> None:
    item = make_workspaceitem(files=[{"name": "thesis.pdf", "sizeBytes": 1024}])

    assert form_for(item).upload_files[0]["name"] == "thesis.pdf"
    assert form_for(item).license_granted is False


# ---- validation errors ------------------------------------------------------


def test_dspace_validation_errors_land_on_their_fields() -> None:
    form = form_for()

    title = form.field("traditionalpageone", "dc.title")
    assert title.errors == ["This field is required."]
    assert message_for("error.validation.required") == "This field is required."


def test_missing_required_fields_are_listed() -> None:
    form = form_for()

    assert {f.config.metadata_field for f in form.missing_required} == {
        "dc.title",
        "dc.date.issued",
        "dc.type",
    }


def test_a_filled_required_field_is_not_missing() -> None:
    item = make_workspaceitem(
        sections={"traditionalpageone": {"dc.title": metadata_values("Filled in")}}
    )

    assert not form_for(item).field("traditionalpageone", "dc.title").required_empty


# ---- pre-fill ---------------------------------------------------------------


def test_extracted_values_land_in_the_section_that_declares_the_field() -> None:
    form = form_for(
        mapped=mapped(
            title=text("Family at the Heart", 0.85, SourceTier.LLM),
            abstract=text("An abstract.", 0.85, SourceTier.LLM),
        )
    )

    title = form.field("traditionalpageone", "dc.title")
    abstract = form.field("traditionalpagetwo", "dc.description.abstract")
    assert title.values[0].value == "Family at the Heart"
    assert title.prefilled
    # Nothing hardcodes which page the abstract lives on -- the config says.
    assert abstract.values[0].value == "An abstract."


def test_a_repeatable_field_takes_every_value() -> None:
    form = form_for(mapped=mapped(authors=items(["Chew, Kari", "Doe, Jane"], 0.85, SourceTier.LLM)))

    field = form.field("traditionalpageone", "dc.contributor.author")
    assert [v.value for v in field.values] == ["Chew, Kari", "Doe, Jane"]


def test_a_single_valued_field_keeps_the_first_and_reports_the_rest() -> None:
    """dc.title is configured non-repeatable, so a multi-valued extraction cannot
    all fit -- and what does not fit has to be said out loud."""
    profile = MappingProfile(name="odd", fields={"authors": "dc.title"})
    two_names = map_metadata(
        ExtractedMetadata(authors=items(["First, A", "Second, B"], 0.85, SourceTier.LLM)),
        profile,
    )

    form = build_form(
        make_workspaceitem(), SUBMISSION_FORMS, sections=DEFINITION_SECTIONS, mapped=two_names
    )

    title = form.field("traditionalpageone", "dc.title")
    assert [v.value for v in title.values] == ["First, A"]
    assert [u.values for u in form.unplaced if "takes only one value" in u.reason] == [
        ["Second, B"]
    ]


def test_low_confidence_values_are_flagged() -> None:
    form = form_for(mapped=mapped(title=text("A guessed title", 0.3, SourceTier.HEURISTIC)))

    assert form.field("traditionalpageone", "dc.title").low_confidence


def test_prefill_never_clobbers_a_stored_value() -> None:
    """Resuming a draft must not overwrite what the user already typed."""
    item = make_workspaceitem(
        sections={"traditionalpageone": {"dc.title": metadata_values("What I typed")}}
    )

    form = build_form(
        item,
        SUBMISSION_FORMS,
        sections=DEFINITION_SECTIONS,
        mapped=mapped(title=text("What the PDF said", 0.85, SourceTier.LLM)),
    )

    assert form.field("traditionalpageone", "dc.title").values[0].value == "What I typed"
    assert any("already has a value" in u.reason for u in form.unplaced)


def test_a_mapped_field_with_no_home_is_reported_not_dropped() -> None:
    """SHAREOK maps committee members; the shipped traditional form has no such
    field. The value must still be visible to the user."""
    form = form_for(mapped=mapped(committee_members=items(["Dr. A, Chair"], 0.85, SourceTier.LLM)))

    unplaced = [u for u in form.unplaced if "committeeMember" in u.label]
    assert unplaced and unplaced[0].values == ["Dr. A, Chair"]
    assert "no field for it" in unplaced[0].reason


def test_an_unmapped_extracted_field_is_reported_too() -> None:
    form = form_for(mapped=mapped(degree=text("Ph.D.", 0.85, SourceTier.LLM)))

    assert [u.values for u in form.unplaced if u.label == "degree"] == [["Ph.D."]]


def test_prefilled_count_counts_fields_not_values() -> None:
    form = form_for(
        mapped=mapped(
            title=text("T", 0.85, SourceTier.LLM),
            authors=items(["A", "B"], 0.85, SourceTier.LLM),
        )
    )

    assert form.prefilled_count == 2


# ---- vocabularies -----------------------------------------------------------


def test_dropdown_fields_carry_their_options() -> None:
    from tests.fakes import VOCABULARIES

    form = build_form(
        make_workspaceitem(),
        SUBMISSION_FORMS,
        sections=DEFINITION_SECTIONS,
        vocabularies=VOCABULARIES,
    )

    field = form.field("traditionalpageone", "dc.type")
    assert [o.value for o in field.options] == ["Article", "Thesis"]


# ---- input naming -----------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "metadata_field", "expected"),
    [
        ("traditionalpageone", "dc.title", "traditionalpageone::dc.title"),
        ("traditionalpagetwo", "dc.subject", "traditionalpagetwo::dc.subject"),
    ],
)
def test_input_names_are_section_qualified(section, metadata_field, expected) -> None:
    """Two sections can configure the same metadata field, so the section has to
    be part of the name Phase 5 will parse."""
    assert form_for().field(section, metadata_field).name == expected


def test_a_definition_that_did_not_arrive_still_renders_the_data_sections() -> None:
    """Last resort: no resolved sections and nothing embedded. A form with
    derived headings beats a blank page."""
    item = make_workspaceitem()
    item.submission_definition = None

    form = build_form(item, SUBMISSION_FORMS)

    assert [s.id for s in form.sections] == list(item.sections)
    assert form.section("traditionalpageone").editable
