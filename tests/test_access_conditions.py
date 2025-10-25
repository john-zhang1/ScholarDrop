"""Embargo and access conditions (DESIGN.md §13.5).

The rule underneath every test here: **the repository decides what is on offer**.
ScholarDrop renders the collection's own upload configuration and writes back
exactly what it named — a collection that configures no conditions gets no
picker, and a condition nobody configured is never sent.
"""

from __future__ import annotations

from html import unescape

import pytest

from scholardrop.submission.access import (
    DEFAULT_DESCRIPTION,
    AccessChoice,
    AccessOption,
    build_operations,
    describe_choices,
    parse_choices,
    parse_options,
    rows_for,
    validate_choices,
)
from scholardrop.submission.form import UploadedFile
from tests.fakes import UPLOAD_CONFIG, make_workspaceitem, upload_file

EMBARGO = AccessOption(name="embargo", has_start_date=True, max_start_date="2029-07-30")
LEASE = AccessOption(name="lease", has_end_date=True, max_end_date="2027-01-30")
OPEN = AccessOption(name="openaccess")
OPTIONS = [OPEN, LEASE, EMBARGO]

CHOICE = "access::0"
DATE = "access::0::date"

ONE_FILE = [UploadedFile(name="thesis.pdf")]
ONE_EMBARGOED_FILE = [
    UploadedFile(name="thesis.pdf", condition="embargo", condition_date="2028-01-01")
]


def post(client, path: str, pairs: list[tuple[str, str]]):
    data: dict[str, list[str]] = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return client.post(path, data=data)


COMPLETE = [
    ("traditionalpageone::dc.title", "An Embargoed Thesis"),
    ("traditionalpageone::dc.date.issued", "2026"),
    ("traditionalpageone::dc.type", "Thesis"),
    ("license_granted", "true"),
]


@pytest.fixture
def with_file(fake_dspace):
    """A draft with one file, which is what a picker is rendered against."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem("42", files=[upload_file("thesis.pdf")])
    return fake_dspace


@pytest.fixture
def with_embargoed_file(fake_dspace):
    """A draft whose file already carries a condition -- chosen here in an
    earlier visit, or in DSpace's own submission UI."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42",
        files=[
            upload_file(
                "thesis.pdf",
                conditions=[{"name": "embargo", "startDate": "2028-06-01T00:00:00Z"}],
            )
        ],
    )
    return fake_dspace


# ---- reading the configuration ---------------------------------------------


def test_options_come_from_the_upload_configuration() -> None:
    options = parse_options(UPLOAD_CONFIG)

    assert [o.name for o in options] == ["openaccess", "lease", "embargo", "administrator"]
    embargo = options[2]
    assert embargo.has_start_date is True and embargo.has_end_date is False
    assert embargo.max_date == "2029-07-30"


def test_a_configuration_with_no_conditions_offers_none() -> None:
    assert parse_options({"name": "upload"}) == []
    assert parse_options(None) == []


def test_the_date_asked_for_depends_on_which_date_it_is() -> None:
    """A start date is when the file opens; an end date is when it closes."""
    assert EMBARGO.date_label == "Restricted until"
    assert LEASE.date_label == "Open until"
    assert OPEN.needs_date is False


def test_a_condition_describes_itself_in_words() -> None:
    assert EMBARGO.describe("2028-01-01") == "Embargoed — restricted until 2028-01-01"
    assert LEASE.describe("2026-12-01") == "Open for a limited time — open until 2026-12-01"
    assert OPEN.describe("") == "Open access"


def test_an_unknown_condition_name_is_shown_as_it_is() -> None:
    assert AccessOption(name="ouEmbargo").label == "ouEmbargo"


# ---- the four rules DSpace applies -----------------------------------------


def test_a_condition_that_takes_a_date_needs_one() -> None:
    assert "needs a date" in EMBARGO.validate("")


def test_a_condition_that_takes_none_must_not_have_one() -> None:
    assert "does not take a date" in OPEN.validate("2028-01-01")


def test_a_date_beyond_the_configured_limit_is_refused() -> None:
    assert "cannot go beyond 2029-07-30" in EMBARGO.validate("2030-01-01")
    assert EMBARGO.validate("2029-07-30") == ""


def test_something_that_is_not_a_date_is_refused() -> None:
    assert "not a date" in EMBARGO.validate("next tuesday")


def test_a_condition_this_collection_does_not_offer_is_refused() -> None:
    choices = {0: AccessChoice(index=0, name="somethingelse")}

    assert validate_choices(choices, OPTIONS, files=ONE_FILE) == {
        0: "That is not an access condition this collection offers."
    }


def test_a_choice_for_a_file_the_draft_no_longer_has_is_refused() -> None:
    """Refused, never dropped: a silently dropped choice once let the deposited
    page claim an embargo the repository was not asked for."""
    choices = {5: AccessChoice(index=5, name="embargo", chosen_date="2028-01-01")}

    assert validate_choices(choices, OPTIONS, files=ONE_FILE) == {
        5: "This draft no longer has that file."
    }


def test_the_explicit_default_is_always_applicable() -> None:
    """It asks the repository for nothing the repository has not already decided."""
    assert validate_choices({0: AccessChoice(index=0, name="")}, OPTIONS, files=ONE_FILE) == {}


# ---- reading the form ------------------------------------------------------


def test_a_choice_and_its_date_are_read_together() -> None:
    choices = parse_choices([(CHOICE, "embargo"), (DATE, "2028-01-01")])

    assert choices == {0: AccessChoice(index=0, name="embargo", chosen_date="2028-01-01")}


def test_an_unposted_file_contributes_nothing() -> None:
    assert parse_choices([("traditionalpageone::dc.title", "A Thesis")]) == {}


def test_an_explicit_default_is_a_choice_not_an_absence() -> None:
    """`access::0=` is somebody choosing the collection's default -- the only way
    to step a file back from a stored condition, so dropping it makes that
    choice unmakeable."""
    choices = parse_choices([(CHOICE, ""), (DATE, "2028-01-01")])

    assert choices == {0: AccessChoice(index=0, name="", chosen_date="2028-01-01")}


def test_junk_input_names_are_ignored() -> None:
    """The index goes into the patch path that names which file gets the policy."""
    posted = [
        ("access::", "embargo"),
        ("access::x", "embargo"),
        ("access::٣", "embargo"),
        ("access::1::nonsense", "embargo"),
        ("traditionalpageone::dc.title", "A Thesis"),
    ]

    assert parse_choices(posted) == {}


# ---- the patch -------------------------------------------------------------


def test_the_patch_names_the_section_the_definition_named() -> None:
    choices = {0: AccessChoice(index=0, name="embargo", chosen_date="2028-01-01")}

    operations = build_operations("upload", choices, OPTIONS, files=ONE_FILE)

    assert operations == [
        {
            "op": "add",
            "path": "/sections/upload/files/0/accessConditions",
            "value": [{"name": "embargo", "startDate": "2028-01-01"}],
        }
    ]


def test_a_lease_carries_an_end_date_instead() -> None:
    choices = {0: AccessChoice(index=0, name="lease", chosen_date="2026-12-01")}

    operations = build_operations("upload", choices, OPTIONS, files=ONE_FILE)

    assert operations[0]["value"] == [{"name": "lease", "endDate": "2026-12-01"}]


def test_a_dateless_condition_carries_only_its_name() -> None:
    choices = {0: AccessChoice(index=0, name="openaccess")}

    operations = build_operations("upload", choices, OPTIONS, files=ONE_FILE)

    assert operations[0]["value"] == [{"name": "openaccess"}]


def test_a_choice_for_a_file_that_is_not_there_is_dropped() -> None:
    """The index is a position in the bundle; one past the end is not a file."""
    choices = {3: AccessChoice(index=3, name="openaccess")}

    assert build_operations("upload", choices, OPTIONS, files=ONE_FILE) == []


def test_an_unconfigured_condition_is_never_sent() -> None:
    choices = {0: AccessChoice(index=0, name="somethingelse")}

    assert build_operations("upload", choices, OPTIONS, files=ONE_FILE) == []


def test_an_explicit_default_clears_a_stored_condition() -> None:
    """The empty array is the clear: `TYPE_CUSTOM` policies are removed before
    the array is read, and an empty one gives the operation nothing to re-apply."""
    choices = {0: AccessChoice(index=0, name="")}

    assert build_operations("upload", choices, OPTIONS, files=ONE_EMBARGOED_FILE) == [
        {"op": "add", "path": "/sections/upload/files/0/accessConditions", "value": []}
    ]


def test_an_explicit_default_over_no_condition_patches_nothing() -> None:
    """For a file with nothing stored, the default is what not patching means."""
    choices = {0: AccessChoice(index=0, name="")}

    assert build_operations("upload", choices, OPTIONS, files=ONE_FILE) == []


def test_an_untouched_picker_restates_and_nothing_is_sent() -> None:
    """The select posts its value whether or not anyone touched it. Re-*setting*
    the stored condition would delete any second condition stacked in DSpace's
    own UI, so a deposit that changed nothing must change nothing."""
    choices = {0: AccessChoice(index=0, name="embargo", chosen_date="2028-01-01")}

    assert build_operations("upload", choices, OPTIONS, files=ONE_EMBARGOED_FILE) == []


def test_a_changed_date_is_not_a_restatement() -> None:
    choices = {0: AccessChoice(index=0, name="embargo", chosen_date="2029-01-01")}

    operations = build_operations("upload", choices, OPTIONS, files=ONE_EMBARGOED_FILE)

    assert operations[0]["value"] == [{"name": "embargo", "startDate": "2029-01-01"}]


def test_a_stored_condition_the_collection_no_longer_offers_may_be_kept() -> None:
    """Keeping what is already there is not a new ask, so it validates and sends
    nothing -- even with no date posted, because an option the collection does
    not configure renders no date box to post one from."""
    stored = [UploadedFile(name="thesis.pdf", condition="ouLegacy", condition_date="2028-01-01")]
    choices = {0: AccessChoice(index=0, name="ouLegacy")}

    assert validate_choices(choices, OPTIONS, files=stored) == {}
    assert build_operations("upload", choices, OPTIONS, files=stored) == []


# ---- the rows the template renders -----------------------------------------


def test_no_configured_options_means_no_rows_at_all() -> None:
    assert rows_for([upload_file("a.pdf")], []) == []


def test_a_stored_condition_is_what_the_row_shows() -> None:
    from scholardrop.submission.form import UploadedFile

    file = UploadedFile.from_payload(
        {
            "metadata": {"dc.title": [{"value": "thesis.pdf"}]},
            "accessConditions": [{"name": "embargo", "startDate": "2028-01-01"}],
        }
    )

    row = rows_for([file], OPTIONS)[0]

    assert row.chosen_name == "embargo"
    assert row.chosen_date == "2028-01-01"
    assert row.selected is EMBARGO or row.selected.name == "embargo"


def test_what_was_just_posted_beats_what_is_stored() -> None:
    from scholardrop.submission.form import UploadedFile

    file = UploadedFile.from_payload(
        {"accessConditions": [{"name": "embargo", "startDate": "2028-01-01"}]}
    )

    row = rows_for([file], OPTIONS, choices={0: AccessChoice(index=0, name="openaccess")})[0]

    assert row.chosen_name == "openaccess"
    assert row.chosen_date == ""


def test_the_explicit_default_beats_what_is_stored() -> None:
    """Choosing "the collection's default" over a stored embargo must stick --
    truthiness on the choice put the embargo straight back."""
    row = rows_for(ONE_EMBARGOED_FILE, OPTIONS, choices={0: AccessChoice(index=0, name="")})[0]

    assert row.chosen_name == ""
    assert row.explicit is True
    assert row.description == DEFAULT_DESCRIPTION


def test_the_row_describes_what_will_be_applied() -> None:
    choices = {0: AccessChoice(index=0, name="embargo", chosen_date="2028-01-01")}

    assert describe_choices(choices, OPTIONS, files=ONE_FILE) == {
        0: "Embargoed — restricted until 2028-01-01"
    }


def test_nothing_is_described_that_was_not_sent() -> None:
    """Same bounds as the patch builder, or the deposited page invents a promise."""
    choices = {5: AccessChoice(index=5, name="embargo", chosen_date="2028-01-01")}

    assert describe_choices(choices, OPTIONS, files=ONE_FILE) == {}


# ---- through the routes ----------------------------------------------------


def test_the_review_form_offers_the_collections_conditions(with_file, with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert 'name="access::0"' in body
    assert "Embargoed" in body and "Open access" in body
    assert "the collection's default" in body


def test_a_collection_with_no_conditions_gets_no_picker(with_file, with_collection) -> None:
    with_file.upload_config = {"name": "upload"}

    body = with_collection.get("/review/42").text

    assert "access::0" not in body
    assert "Who may read this file" not in body


def test_the_slice_can_be_switched_off(app, with_file, with_collection) -> None:
    app.state.settings.access_conditions_enabled = False

    body = with_collection.get("/review/42").text

    assert "access::0" not in body
    assert not with_file.called("submission_upload")


def test_a_draft_with_no_file_has_nothing_to_restrict(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert "access::0" not in body


def test_picking_a_condition_asks_for_the_date_it_takes(with_file, with_collection) -> None:
    body = with_collection.get("/review/42/access?index=0&option=embargo").text

    assert "Restricted until" in body
    assert 'max="2029-07-30"' in body
    assert 'name="access::0::date"' in body


def test_a_dateless_condition_swaps_in_nothing(with_file, with_collection) -> None:
    """Which is how choosing open access clears a date box that was there."""
    body = with_collection.get("/review/42/access?index=0&option=openaccess").text

    assert body.strip() == ""


def test_the_confirm_page_states_and_carries_the_choice(with_file, with_collection) -> None:
    body = post(
        with_collection,
        "/review/42/confirm",
        [*COMPLETE, (CHOICE, "embargo"), (DATE, "2028-01-01")],
    ).text

    assert "Embargoed — restricted until 2028-01-01" in body
    assert f'name="{CHOICE}" value="embargo"' in body
    assert f'name="{DATE}" value="2028-01-01"' in body


def test_the_deposit_patches_the_access_condition(with_file, with_collection) -> None:
    post(
        with_collection,
        "/review/42/deposit",
        [*COMPLETE, (CHOICE, "embargo"), (DATE, "2028-01-01")],
    )

    operations = [op for patch in with_file.patches for op in patch]
    assert {
        "op": "add",
        "path": "/sections/upload/files/0/accessConditions",
        "value": [{"name": "embargo", "startDate": "2028-01-01"}],
    } in operations


def test_the_deposited_page_says_what_was_applied(with_file, with_collection) -> None:
    body = post(
        with_collection,
        "/review/42/deposit",
        [*COMPLETE, (CHOICE, "embargo"), (DATE, "2028-01-01")],
    ).text

    assert "Embargoed — restricted until 2028-01-01" in body
    assert "thesis.pdf" in body


def test_an_embargo_with_no_date_never_reaches_the_repository(with_file, with_collection) -> None:
    response = post(with_collection, "/review/42/deposit", [*COMPLETE, (CHOICE, "embargo")])

    assert response.status_code == 400
    assert "needs a date" in response.text
    assert with_file.patches == [], "nothing may be written when the choice is unusable"
    assert not with_file.called("deposit")


def test_a_date_beyond_the_limit_is_refused_before_the_deposit(with_file, with_collection) -> None:
    response = post(
        with_collection,
        "/review/42/deposit",
        [*COMPLETE, (CHOICE, "embargo"), (DATE, "2031-01-01")],
    )

    assert response.status_code == 400
    assert "cannot go beyond 2029-07-30" in response.text
    assert with_file.patches == []


def test_the_refused_form_comes_back_with_the_choice_intact(with_file, with_collection) -> None:
    body = post(with_collection, "/review/42/deposit", [*COMPLETE, (CHOICE, "embargo")]).text

    assert '<option value="embargo" selected>' in body


def test_choosing_nothing_writes_nothing(with_file, with_collection) -> None:
    """The collection's default applies, and this app must not claim otherwise."""
    body = post(with_collection, "/review/42/deposit", [*COMPLETE, (CHOICE, "")]).text

    operations = [op for patch in with_file.patches for op in patch]
    assert not any("accessConditions" in op["path"] for op in operations)
    assert "Access:" not in body


def test_nothing_is_patched_when_the_slice_is_off(app, with_file, with_collection) -> None:
    app.state.settings.access_conditions_enabled = False

    post(
        with_collection,
        "/review/42/deposit",
        [*COMPLETE, (CHOICE, "embargo"), (DATE, "2028-01-01")],
    )

    operations = [op for patch in with_file.patches for op in patch]
    assert not any("accessConditions" in op["path"] for op in operations)


def test_a_choice_for_a_vanished_file_is_refused_not_dropped(with_file, with_collection) -> None:
    """A file removed in another tab between render and deposit. The old
    behaviour dropped the choice from the patch but still described it on the
    deposited page -- an embargo claimed that the repository was never asked for."""
    response = post(
        with_collection,
        "/review/42/deposit",
        [*COMPLETE, ("access::5", "embargo"), ("access::5::date", "2028-01-01")],
    )

    assert response.status_code == 400
    assert "files on this draft have changed" in response.text
    assert with_file.patches == [], "nothing may be written when the files have changed"
    assert not with_file.called("deposit")


def test_the_default_choice_over_a_stored_condition_clears_it(
    with_embargoed_file, with_collection
) -> None:
    """Stepping a file back from its stored embargo has to actually happen --
    the stored condition once refilled the choice and travelled to the deposit."""
    post(with_collection, "/review/42/deposit", [*COMPLETE, (CHOICE, "")])

    operations = [op for patch in with_embargoed_file.patches for op in patch]
    assert {
        "op": "add",
        "path": "/sections/upload/files/0/accessConditions",
        "value": [],
    } in operations


def test_the_confirm_page_does_not_resurrect_the_stored_condition(
    with_embargoed_file, with_collection
) -> None:
    # unescape: the description's apostrophe renders as &#39;.
    body = unescape(post(with_collection, "/review/42/confirm", [*COMPLETE, (CHOICE, "")]).text)

    assert f'name="{CHOICE}" value=""' in body, "the explicit default must travel to the deposit"
    assert DEFAULT_DESCRIPTION in body
    assert "restricted until" not in body, "the stored embargo must not come back as the choice"


def test_the_deposited_page_says_the_default_replaced_the_condition(
    with_embargoed_file, with_collection
) -> None:
    body = unescape(post(with_collection, "/review/42/deposit", [*COMPLETE, (CHOICE, "")]).text)

    assert DEFAULT_DESCRIPTION in body
    assert "restricted until" not in body


# ---- files that already carry conditions ------------------------------------


def test_a_file_counts_every_condition_it_stores() -> None:
    file = UploadedFile.from_payload(
        {
            "accessConditions": [
                {"name": "embargo", "startDate": "2028-06-01T00:00:00Z"},
                {"name": "administrator"},
            ]
        }
    )

    assert file.condition == "embargo"
    assert file.condition_count == 2


@pytest.fixture
def with_stacked_conditions(fake_dspace):
    """Two conditions on one file -- DSpace's own UI can stack them; this
    picker cannot, and must not silently unstack them either."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42",
        files=[
            upload_file(
                "thesis.pdf",
                conditions=[
                    {"name": "embargo", "startDate": "2028-06-01T00:00:00Z"},
                    {"name": "administrator"},
                ],
            )
        ],
    )
    return fake_dspace


def test_an_untouched_deposit_leaves_a_stacked_file_alone(
    with_stacked_conditions, with_collection
) -> None:
    """The form posts the pre-selected stored condition straight back; sending
    it as an operation would *set* the file's policies and silently delete the
    second condition."""
    post(
        with_collection,
        "/review/42/deposit",
        [*COMPLETE, (CHOICE, "embargo"), (DATE, "2028-06-01")],
    )

    operations = [op for patch in with_stacked_conditions.patches for op in patch]
    assert not any("accessConditions" in op["path"] for op in operations)


def test_the_picker_says_when_a_choice_replaces_several(
    with_stacked_conditions, with_collection
) -> None:
    body = with_collection.get("/review/42").text

    assert "choosing here replaces them all" in body


def test_a_stored_condition_the_collection_no_longer_offers_still_renders(
    fake_dspace, with_collection
) -> None:
    """Without its own <option>, the browser falls back to the default option
    and merely submitting the form clears a condition nobody touched."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42", files=[upload_file("thesis.pdf", conditions=[{"name": "ouLegacy"}])]
    )

    body = with_collection.get("/review/42").text

    assert 'value="ouLegacy" selected' in body
