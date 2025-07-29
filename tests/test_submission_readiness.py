"""What a draft still needs, translated from DSpace's own verdict (Phase 4b).

The rule this file guards: readiness is *reported*, never *computed*. Everything
here starts from the `errors` array DSpace put on the workspaceitem, so the list
cannot claim a draft is ready when the deposit call would refuse it, or nag about
a field the collection does not actually require.
"""

from __future__ import annotations

from scholardrop.submission.readiness import assess, labels_from
from tests.fakes import FORM_PAGE_ONE, FORM_PAGE_TWO, make_workspaceitem

LABELS = labels_from({"traditionalpageone": FORM_PAGE_ONE, "traditionalpagetwo": FORM_PAGE_TWO})


def required(*paths: str) -> list[dict]:
    return [{"message": "error.validation.required", "paths": list(paths)}]


def test_a_draft_with_no_errors_is_ready() -> None:
    state = assess(make_workspaceitem("7", errors=[]), LABELS)

    assert state.ready
    assert state.badge == "Ready"
    assert state.blockers == ()


def test_required_fields_are_named_as_the_collection_names_them() -> None:
    item = make_workspaceitem(
        "7",
        errors=required(
            "/sections/traditionalpageone/dc.title",
            "/sections/traditionalpagetwo/dc.description.abstract",
        ),
    )

    state = assess(item, LABELS)

    assert [b.label for b in state.blockers] == ["Title", "Abstract"]
    assert all(b.message == "This field is required." for b in state.blockers)
    assert state.badge == "Needs 2"


def test_missing_and_wrong_are_different_words() -> None:
    """ "Needs 2" is a to-do; "1 problem" is a correction. Don't conflate them."""
    missing = assess(make_workspaceitem("7", errors=required("/sections/x/dc.title")), LABELS)
    wrong = assess(
        make_workspaceitem(
            "7",
            errors=[{"message": "error.validation.date", "paths": ["/sections/x/dc.date.issued"]}],
        ),
        LABELS,
    )

    assert missing.badge == "Needs 1"
    assert missing.missing_only
    assert wrong.badge == "1 problem"
    assert not wrong.missing_only


def test_section_level_errors_name_the_section() -> None:
    item = make_workspaceitem(
        "7",
        errors=[
            {"message": "error.validation.filerequired", "paths": ["/sections/upload"]},
            {"message": "error.validation.license.notgranted", "paths": ["/sections/license"]},
        ],
    )

    state = assess(item, LABELS)

    assert [b.label for b in state.blockers] == ["File", "Deposit licence"]
    assert state.missing_only, "neither is a mistake; both are things not done yet"


def test_an_unknown_message_key_is_never_shown_raw() -> None:
    """A key next to a form field is searchable; in a list it is just noise."""
    item = make_workspaceitem(
        "7",
        errors=[{"message": "error.validation.something.new", "paths": ["/sections/x/dc.title"]}],
    )

    state = assess(item, LABELS)

    assert "error.validation" not in state.blockers[0].message
    assert state.blockers[0].label == "Title"


def test_the_same_field_failing_twice_is_listed_once() -> None:
    item = make_workspaceitem(
        "7",
        errors=[
            *required("/sections/traditionalpageone/dc.title"),
            *required("/sections/traditionalpageone/dc.title"),
        ],
    )

    assert len(assess(item, LABELS).blockers) == 1


def test_an_error_with_no_path_is_still_reported() -> None:
    item = make_workspaceitem("7", errors=[{"message": "error.validation.required"}])

    state = assess(item, LABELS)

    assert len(state.blockers) == 1
    assert state.blockers[0].label == ""
    assert state.blockers[0].text == "This field is required."


def test_without_labels_the_field_name_is_shown_and_said_to_be_raw() -> None:
    """Unreadable form config degrades the wording, never the verdict."""
    item = make_workspaceitem("7", errors=required("/sections/traditionalpageone/dc.title"))

    state = assess(item, labels=None, labelled=False)

    assert state.blockers[0].label == "dc.title"
    assert state.labelled is False


def test_a_ready_draft_is_never_flagged_as_unlabelled() -> None:
    """There is nothing to mislabel, so the caveat would only confuse."""
    state = assess(make_workspaceitem("7", errors=[]), labels=None, labelled=False)

    assert state.ready
    assert state.labelled is True


def test_long_lists_stop_listing_and_start_counting() -> None:
    item = make_workspaceitem("7", errors=required(*[f"/sections/x/dc.f{i}" for i in range(7)]))

    state = assess(item, LABELS)

    assert len(state.shown) == 3
    assert state.hidden == 4
    assert state.badge == "Needs 7"


def test_labels_are_keyed_both_ways() -> None:
    """A section-qualified path wins; a bare field name still resolves."""
    assert LABELS["traditionalpageone/dc.title"] == "Title"
    assert LABELS["dc.title"] == "Title"
    assert LABELS["traditionalpagetwo/dc.subject"] == "Subject Keywords"
