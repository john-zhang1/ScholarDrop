"""Confirm and deposit (DESIGN.md §6 steps 7-8).

The promises being tested: nothing is written before the last step, the exact
patch and deposit calls are the ones intended, a refusal leaves the draft intact
and comes back as the same form with the same edits, and the confirmation tells
the truth about which of the two things DSpace did.
"""

from __future__ import annotations

from scholardrop.dspace import (
    DepositResult,
    DSpaceUnavailable,
    DSpaceValidationError,
    ValidationIssue,
    WorkflowItem,
)
from tests.fakes import make_workspaceitem, metadata_values

TITLE = "traditionalpageone::dc.title"
AUTHOR = "traditionalpageone::dc.contributor.author"
TYPE = "traditionalpageone::dc.type"
DATE = "traditionalpageone::dc.date.issued"

COMPLETE = [
    (TITLE, "Family at the Heart"),
    (AUTHOR, "Chew, Kari"),
    (DATE, "2016"),
    (TYPE, "Thesis"),
    ("license_granted", "true"),
]


def post(client, path: str, pairs: list[tuple[str, str]]):
    """POST form pairs, repeated names included.

    httpx wants {name: [values]} for a repeated field -- handed a list of tuples
    it posts them as a raw body instead, and the form arrives empty.
    """
    data: dict[str, list[str]] = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return client.post(path, data=data)


# ---- the confirm step ------------------------------------------------------


def test_the_review_form_posts_to_confirm(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert 'action="/review/42/confirm"' in body
    assert "Review &amp; submit" in body


def test_the_licence_text_and_checkbox_are_on_the_review_page(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert 'name="license_granted"' in body
    assert "NON-EXCLUSIVE DISTRIBUTION LICENSE" in body


def test_confirm_summarises_without_writing_anything(with_collection, fake_dspace) -> None:
    response = post(with_collection, "/review/42/confirm", COMPLETE)

    assert response.status_code == 200
    body = response.text
    assert "Ready to deposit" in body
    assert "OU - Dissertations" in body  # collection
    assert "Family at the Heart" in body  # metadata
    assert "Granted" in body  # licence
    assert not fake_dspace.called("patch_sections")
    assert not fake_dspace.called("deposit")


def test_confirm_carries_every_value_forward(with_collection) -> None:
    body = post(with_collection, "/review/42/confirm", COMPLETE).text

    assert f'<input type="hidden" name="{TITLE}" value="Family at the Heart">' in body
    assert f'<input type="hidden" name="{AUTHOR}" value="Chew, Kari">' in body
    assert '<input type="hidden" name="license_granted" value="true">' in body
    assert 'action="/review/42/deposit"' in body


def test_confirm_carries_a_cleared_field_as_an_empty_input(with_collection, fake_dspace) -> None:
    """Otherwise "I deleted this value" is lost between the two steps."""
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42", sections={"traditionalpageone": {"dc.title": metadata_values("Old title")}}
    )

    body = post(
        with_collection, "/review/42/confirm", [(TITLE, ""), ("license_granted", "true")]
    ).text

    assert f'<input type="hidden" name="{TITLE}" value="">' in body
    assert "Old title" not in body


def test_confirm_warns_about_missing_required_fields(with_collection) -> None:
    body = post(with_collection, "/review/42/confirm", [(TITLE, "Only a title")]).text

    assert "requires" in body and "more field" in body
    assert "not granted" in body


def test_back_to_editing_keeps_the_edits(with_collection) -> None:
    response = post(with_collection, "/review/42/edit", COMPLETE)

    assert response.status_code == 200
    assert 'value="Family at the Heart"' in response.text
    compact = " ".join(response.text.split())
    assert 'name="license_granted" value="true" checked' in compact


# ---- the deposit -----------------------------------------------------------


def test_deposit_patches_then_deposits_with_the_exact_calls(with_collection, fake_dspace) -> None:
    response = post(with_collection, "/review/42/deposit", COMPLETE)

    assert response.status_code == 200
    assert fake_dspace.patches == [
        [
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
                "path": "/sections/traditionalpageone/dc.date.issued",
                "value": [{"value": "2016"}],
            },
            {
                "op": "add",
                "path": "/sections/traditionalpageone/dc.type",
                "value": [{"value": "Thesis"}],
            },
            {"op": "add", "path": "/sections/license/granted", "value": "true"},
        ]
    ]
    args, _ = fake_dspace.call_args("deposit")
    assert args[0].endswith("/submission/workspaceitems/42")


def test_entering_the_workflow_says_so_with_its_reference(with_collection) -> None:
    body = post(with_collection, "/review/42/deposit", COMPLETE).text

    assert "Submitted for review" in body
    assert "9001" in body
    assert "Submit another" in body


def test_archived_directly_shows_the_handle(with_collection, fake_dspace) -> None:
    fake_dspace.deposit_result = DepositResult(archived=True)

    body = post(with_collection, "/review/42/deposit", COMPLETE).text

    assert "Deposited" in body
    assert "hdl.handle.net/11244/9999" in body
    # Twice: once before the deposit to learn the item uuid while the
    # workspaceitem still exists, once after to read the freshly minted handle.
    assert [c[0] for c in fake_dspace.calls].count("item") == 2


def test_a_deposit_that_archives_survives_an_unreadable_item(with_collection, fake_dspace) -> None:
    fake_dspace.deposit_result = DepositResult(archived=True)
    fake_dspace.raise_on["item"] = DSpaceUnavailable("boom", status=500)

    response = post(with_collection, "/review/42/deposit", COMPLETE)

    assert response.status_code == 200
    assert "Deposited" in response.text


def test_the_draft_leaves_the_session_once_it_is_deposited(with_collection, app, pdf_bytes) -> None:
    with_collection.post(
        "/submit",
        files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")},
        headers={"HX-Request": "true"},
    )
    session = next(iter(app.state.sessions._sessions.values()))
    assert session.workspaceitem_id == "42"

    post(with_collection, "/review/42/deposit", COMPLETE)

    assert session.workspaceitem_id is None
    assert app.state.extractions.get(session.id, "42") is None


# ---- refusals leave the draft intact ---------------------------------------


def test_without_the_licence_nothing_is_written(with_collection, fake_dspace) -> None:
    response = post(with_collection, "/review/42/deposit", COMPLETE[:-1])

    assert response.status_code == 400
    assert "deposit licence has to be granted" in response.text
    assert not fake_dspace.called("patch_sections")
    assert not fake_dspace.called("deposit")


def test_an_incomplete_submission_comes_back_as_the_form_not_a_deposit(
    with_collection, fake_dspace
) -> None:
    fake_dspace.errors_after_patch = [
        {
            "message": "error.validation.required",
            "paths": ["/sections/traditionalpageone/dc.date.issued"],
        }
    ]

    response = post(with_collection, "/review/42/deposit", COMPLETE)

    assert response.status_code == 400
    assert not fake_dspace.called("deposit"), "an incomplete item must never be deposited"
    body = response.text
    assert "not complete yet" in body
    assert "This field is required." in body
    # the user's own values are still in the form
    assert 'value="Family at the Heart"' in body


def test_a_rejected_deposit_marks_the_offending_fields(with_collection, fake_dspace) -> None:
    fake_dspace.raise_on["deposit"] = DSpaceValidationError(
        "rejected",
        issues=[
            ValidationIssue("error.validation.required", ["/sections/traditionalpageone/dc.title"])
        ],
        status=422,
    )

    response = post(with_collection, "/review/42/deposit", COMPLETE)

    assert response.status_code == 400
    body = response.text
    assert "refused the deposit" in body
    assert "your draft is still" in body
    assert "This field is required." in body


def test_a_repository_failure_leaves_the_draft_and_says_so(with_collection, fake_dspace) -> None:
    fake_dspace.raise_on["deposit"] = DSpaceUnavailable("boom", status=500)

    response = post(with_collection, "/review/42/deposit", COMPLETE)

    assert response.status_code == 502
    assert "Your draft is intact" in response.text
    assert 'value="Family at the Heart"' in response.text


def test_a_rejected_patch_does_not_deposit(with_collection, fake_dspace) -> None:
    fake_dspace.raise_on["patch_sections"] = DSpaceValidationError(
        "bad value",
        issues=[
            ValidationIssue("error.validation.required", ["/sections/traditionalpageone/dc.type"])
        ],
        status=422,
    )

    response = post(with_collection, "/review/42/deposit", COMPLETE)

    assert response.status_code == 400
    assert not fake_dspace.called("deposit")
    assert "Your draft is intact" in response.text


def test_deposit_needs_a_session(web) -> None:
    assert post(web, "/review/42/deposit", COMPLETE).status_code == 303


def test_a_deposit_result_with_no_workflow_item_still_renders(with_collection, fake_dspace) -> None:
    fake_dspace.deposit_result = DepositResult(archived=False, workflow_item=None)

    body = post(with_collection, "/review/42/deposit", COMPLETE).text

    assert "Submitted for review" in body


def test_the_workflow_reference_comes_from_dspace(with_collection, fake_dspace) -> None:
    fake_dspace.deposit_result = DepositResult(
        archived=False, workflow_item=WorkflowItem(id="12345", self_link="")
    )

    assert "12345" in post(with_collection, "/review/42/deposit", COMPLETE).text
