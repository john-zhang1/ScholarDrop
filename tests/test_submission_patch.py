"""Form -> JSON Patch (DESIGN.md §3).

The operation choices encode what DSpace 9.2 actually does, probed on 2026-07-28:
`add` with a whole array *sets* a field (a second `add` replaces, it does not
append), `remove` clears and is a safe no-op when the field is absent, and
`replace` answers 500. If those assumptions ever change, these tests are the
record of what was true.
"""

from __future__ import annotations

from scholardrop.submission.patch import (
    build_operations,
    field_index,
    license_granted,
    parse_submission,
)
from tests.fakes import DEFINITION_SECTIONS, SUBMISSION_FORMS, make_workspaceitem, metadata_values

ALLOWED = field_index(DEFINITION_SECTIONS, SUBMISSION_FORMS)
TITLE = "traditionalpageone::dc.title"
AUTHOR = "traditionalpageone::dc.contributor.author"
ABSTRACT = "traditionalpagetwo::dc.description.abstract"


# ---- reading the POST -------------------------------------------------------


def test_values_are_grouped_by_field_in_submitted_order() -> None:
    submitted = parse_submission(
        [(AUTHOR, "First, A"), (TITLE, "A Thesis"), (AUTHOR, "Second, B")], ALLOWED
    )

    assert submitted[("traditionalpageone", "dc.contributor.author")] == [
        "First, A",
        "Second, B",
    ]
    assert submitted[("traditionalpageone", "dc.title")] == ["A Thesis"]


def test_values_are_stripped_and_blanks_dropped_but_the_field_is_still_seen() -> None:
    """A cleared field must survive as an empty list -- that is what tells the
    patch builder to remove it, rather than to leave it alone."""
    submitted = parse_submission([(TITLE, "   "), (AUTHOR, "  Chew, Kari  ")], ALLOWED)

    assert submitted[("traditionalpageone", "dc.title")] == []
    assert submitted[("traditionalpageone", "dc.contributor.author")] == ["Chew, Kari"]


def test_fields_this_collection_does_not_configure_are_ignored() -> None:
    """A crafted POST cannot write a metadata field the form does not offer."""
    submitted = parse_submission(
        [("traditionalpageone::dc.rights.holder", "me"), ("nonsense", "x"), (TITLE, "ok")],
        ALLOWED,
    )

    assert list(submitted) == [("traditionalpageone", "dc.title")]


def test_the_licence_checkbox_is_present_or_absent() -> None:
    assert license_granted([("license_granted", "true")]) is True
    assert license_granted([(TITLE, "A Thesis")]) is False


# ---- building the patch -----------------------------------------------------


def test_a_new_value_is_one_add_with_the_whole_array() -> None:
    item = make_workspaceitem(errors=[])

    operations = build_operations(
        item, parse_submission([(AUTHOR, "First, A"), (AUTHOR, "Second, B")], ALLOWED)
    )

    assert operations == [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.contributor.author",
            "value": [{"value": "First, A"}, {"value": "Second, B"}],
        }
    ]


def test_an_unchanged_field_produces_no_operation() -> None:
    item = make_workspaceitem(
        errors=[], sections={"traditionalpageone": {"dc.title": metadata_values("A Thesis")}}
    )

    assert build_operations(item, parse_submission([(TITLE, "A Thesis")], ALLOWED)) == []


def test_a_changed_field_is_set_not_appended() -> None:
    item = make_workspaceitem(
        errors=[], sections={"traditionalpageone": {"dc.title": metadata_values("Old")}}
    )

    operations = build_operations(item, parse_submission([(TITLE, "New")], ALLOWED))

    assert operations == [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.title",
            "value": [{"value": "New"}],
        }
    ]


def test_a_cleared_field_is_removed() -> None:
    item = make_workspaceitem(
        errors=[], sections={"traditionalpageone": {"dc.title": metadata_values("Old")}}
    )

    operations = build_operations(item, parse_submission([(TITLE, "")], ALLOWED))

    assert operations == [{"op": "remove", "path": "/sections/traditionalpageone/dc.title"}]


def test_a_field_that_was_empty_and_stayed_empty_is_left_alone() -> None:
    operations = build_operations(
        make_workspaceitem(errors=[]), parse_submission([(TITLE, "")], ALLOWED)
    )

    assert operations == []


def test_reordering_a_repeatable_field_is_a_change() -> None:
    """Author order is meaningful, so a re-order has to be sent."""
    item = make_workspaceitem(
        errors=[],
        sections={"traditionalpageone": {"dc.contributor.author": metadata_values("A", "B")}},
    )

    operations = build_operations(item, parse_submission([(AUTHOR, "B"), (AUTHOR, "A")], ALLOWED))

    assert operations[0]["value"] == [{"value": "B"}, {"value": "A"}]


def test_the_licence_is_granted_as_a_string() -> None:
    operations = build_operations(make_workspaceitem(errors=[]), {}, grant_license=True)

    assert operations == [{"op": "add", "path": "/sections/license/granted", "value": "true"}]


def test_an_already_granted_licence_is_not_granted_twice() -> None:
    item = make_workspaceitem(
        errors=[],
        sections={"license": {"granted": True, "url": "http://x/licence", "acceptanceDate": "now"}},
    )

    assert build_operations(item, {}, grant_license=True) == []


def test_a_whole_form_becomes_one_readable_patch() -> None:
    item = make_workspaceitem(errors=[])

    operations = build_operations(
        item,
        parse_submission(
            [
                (TITLE, "Family at the Heart"),
                (AUTHOR, "Chew, Kari"),
                (ABSTRACT, "An abstract."),
                ("traditionalpageone::dc.date.issued", "2016"),
            ],
            ALLOWED,
        ),
        grant_license=True,
    )

    assert operations == [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.title",
            "value": [{"value": "Family at the Heart"}],
        },
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.contributor.author",
            "value": [{"value": "Chew, Kari"}],
        },
        {
            "op": "add",
            "path": "/sections/traditionalpagetwo/dc.description.abstract",
            "value": [{"value": "An abstract."}],
        },
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.date.issued",
            "value": [{"value": "2016"}],
        },
        {"op": "add", "path": "/sections/license/granted", "value": "true"},
    ]
