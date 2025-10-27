"""The submission status page (DESIGN.md §13.3).

"Where is my item?" -- the question this app was getting asked most after a
deposit, answered from the local log joined with live repository state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholardrop.dspace import DSpaceUnavailable
from scholardrop.dspace.models import ItemStatus
from scholardrop.history.log import Deposit, open_log
from tests.conftest import SHIPPED_INSTANCE_KEY, shipped_base_url
from tests.fakes import make_collection, make_workspaceitem, metadata_values

COMPLETE = {"license_granted": "true", "traditionalpageone::dc.title": "A Logged Thesis"}


@pytest.fixture
def logged(app):
    """Put a row in the log the app is configured to read."""

    def _record(**overrides: object) -> Deposit:
        fields: dict = {
            "base_url": shipped_base_url(app),
            "instance_key": SHIPPED_INSTANCE_KEY,
            "eperson_uuid": "eperson-1",
            "wsi_id": "42",
            "item_uuid": "item-1",
            "title": "A Deposited Thesis",
            "collection_name": "OU - Theses",
            "deposited_at": "2026-07-30T10:00:00+00:00",
        }
        fields.update(overrides)
        deposit = Deposit(**fields)  # type: ignore[arg-type]
        with open_log(app.state.settings.submissions_log_path) as history:
            assert history.record(deposit)
        return deposit

    return _record


def test_the_page_loads_its_list_with_htmx(signed_in) -> None:
    response = signed_in.get("/my-submissions")

    assert response.status_code == 200
    assert 'hx-get="/my-submissions/list"' in response.text


def test_signing_in_is_required(web) -> None:
    assert web.get("/my-submissions").status_code in (303, 307)


def test_an_archived_deposit_is_live_with_its_handle(signed_in, fake_dspace, logged) -> None:
    logged()
    fake_dspace.item_statuses["item-1"] = ItemStatus(
        uuid="item-1", found=True, in_archive=True, handle="11244/345333"
    )

    body = signed_in.get("/my-submissions/list").text

    assert "A Deposited Thesis" in body
    assert "Live" in body
    assert "hdl.handle.net/11244/345333" in body


def test_a_deposit_in_review_says_so_and_names_the_workflow(signed_in, fake_dspace, logged) -> None:
    logged()
    fake_dspace.item_statuses["item-1"] = ItemStatus(
        uuid="item-1", found=True, in_workflow=True, workflow_id="9001"
    )

    body = signed_in.get("/my-submissions/list").text

    assert "In review" in body
    assert "9001" in body
    assert "reviewers" in body


def test_a_returned_submission_is_resumable_at_its_new_draft(
    signed_in, fake_dspace, logged
) -> None:
    """The id in the log names a workspaceitem the deposit consumed. A rejection
    mints a new one, and that is the id "Resume" has to point at."""
    logged(wsi_id="42", item_uuid="item-1")
    fake_dspace.item_statuses["item-1"] = ItemStatus(
        uuid="item-1", found=True, workspaceitem_id="99"
    )

    body = signed_in.get("/my-submissions/list").text

    assert 'href="/review/99"' in body
    assert 'href="/review/42"' not in body


def test_a_withdrawn_item_is_not_reported_as_live(signed_in, fake_dspace, logged) -> None:
    logged()
    fake_dspace.item_statuses["item-1"] = ItemStatus(
        uuid="item-1", found=True, in_archive=True, withdrawn=True, handle="11244/1"
    )

    body = signed_in.get("/my-submissions/list").text

    assert "Withdrawn" in body
    assert "withdrawn from public view" in body


def test_a_removed_item_says_it_is_gone_rather_than_erroring(
    signed_in, fake_dspace, logged
) -> None:
    logged()
    fake_dspace.missing_items.add("item-1")

    response = signed_in.get("/my-submissions/list")

    assert response.status_code == 200
    assert "no longer has this item" in response.text


def test_one_unreadable_row_does_not_take_the_page_down(signed_in, fake_dspace, logged) -> None:
    """Per-row degradation: everything that did resolve still renders."""
    logged(wsi_id="1", item_uuid="item-1", title="The one that answers")
    logged(wsi_id="2", item_uuid="item-2", title="The one that does not")
    fake_dspace.raise_on["item_status"] = DSpaceUnavailable("down", path="/core/items")

    response = signed_in.get("/my-submissions/list")

    assert response.status_code == 200
    assert "could not be checked" in response.text
    assert "The one that answers" in response.text


def test_drafts_appear_even_though_they_were_never_deposited(signed_in, fake_dspace) -> None:
    """§13.3: drafts come from findBySubmitter, so one started in the DSpace UI
    is on this page too."""
    fake_dspace.workspaceitems = {
        "7": make_workspaceitem(
            "7", sections={"traditionalpageone": {"dc.title": metadata_values("Half a thesis")}}
        )
    }

    body = signed_in.get("/my-submissions/list").text

    assert "Half a thesis" in body
    assert "Draft" in body
    assert 'href="/review/7"' in body


def test_a_draft_keeps_its_discard_action(signed_in, fake_dspace) -> None:
    fake_dspace.workspaceitems = {"7": make_workspaceitem("7")}

    body = signed_in.get("/my-submissions/list").text

    assert 'hx-get="/drafts/7/discard"' in body


def test_a_deposit_that_came_back_is_listed_once(signed_in, fake_dspace, logged) -> None:
    """Same submission from both sources -- the live draft is the row that shows."""
    logged(wsi_id="42", item_uuid="item-7")
    fake_dspace.item_statuses["item-7"] = ItemStatus(
        uuid="item-7", found=True, workspaceitem_id="7"
    )
    fake_dspace.workspaceitems = {
        "7": make_workspaceitem(
            "7",
            sections={"traditionalpageone": {"dc.title": metadata_values("Sent back to me")}},
        )
    }

    body = signed_in.get("/my-submissions/list").text

    assert body.count("<li class=") == 1
    assert body.count('id="draft-7"') == 1
    assert "Sent back to me" in body
    assert "A Deposited Thesis" not in body, "the log row is the one that drops out"


def test_another_persons_deposit_is_never_listed(signed_in, fake_dspace, logged) -> None:
    logged(wsi_id="1", item_uuid="item-1", eperson_uuid="somebody-else", title="Not yours")

    body = signed_in.get("/my-submissions/list").text

    assert "Not yours" not in body


def test_a_deposit_at_another_repository_is_never_listed(signed_in, fake_dspace, logged) -> None:
    logged(wsi_id="1", item_uuid="item-1", base_url="https://elsewhere.test/server", title="Away")

    body = signed_in.get("/my-submissions/list").text

    assert "Away" not in body


def test_nothing_at_all_reads_as_nothing_yet(signed_in, fake_dspace) -> None:
    body = signed_in.get("/my-submissions/list").text

    assert "Nothing yet" in body


def test_an_unreadable_log_still_lists_the_drafts(signed_in, fake_dspace, app, tmp_path) -> None:
    """The log is optional. Losing it must not lose the half of the page DSpace
    can still answer."""
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database")
    app.state.settings.submissions_log_path = broken
    fake_dspace.workspaceitems = {
        "7": make_workspaceitem(
            "7", sections={"traditionalpageone": {"dc.title": metadata_values("Still here")}}
        )
    }

    response = signed_in.get("/my-submissions/list")

    assert response.status_code == 200
    assert "Still here" in response.text
    assert "not being recorded" in response.text


def test_switching_the_log_off_leaves_the_page_working(signed_in, fake_dspace, app) -> None:
    app.state.settings.submissions_log_enabled = False
    fake_dspace.workspaceitems = {"7": make_workspaceitem("7")}

    response = signed_in.get("/my-submissions/list")

    assert response.status_code == 200
    assert "Untitled draft" in response.text


def test_the_collection_name_is_shown(signed_in, fake_dspace, logged) -> None:
    logged(collection_name="OU - Dissertations")

    assert "OU - Dissertations" in signed_in.get("/my-submissions/list").text


def test_only_the_most_recent_rows_are_shown(signed_in, fake_dspace, app, logged) -> None:
    app.state.settings.submissions_max_rows = 2
    for n in range(1, 6):
        logged(wsi_id=str(n), item_uuid=f"item-{n}", deposited_at=f"2026-07-0{n}T10:00:00+00:00")

    body = signed_in.get("/my-submissions/list").text

    assert body.count("<li class=") == 2
    assert "Showing the most recent" in body


def test_the_nav_offers_the_page(signed_in) -> None:
    assert 'href="/my-submissions"' in signed_in.get("/drafts").text


def test_the_confirmation_page_points_at_it(with_collection) -> None:
    """The moment somebody most wants to know where their item went."""
    response = with_collection.post("/review/42/deposit", data=COMPLETE)

    assert response.status_code == 200
    assert 'href="/my-submissions"' in response.text


def test_a_deposit_is_logged(with_collection, fake_dspace, app) -> None:
    """The row the status page depends on is written by the deposit itself."""
    fake_dspace.deposit_result = fake_dspace.deposit_result.model_copy(update={"archived": True})
    fake_dspace.archived_item = make_collection("item-42", "T", handle="11244/1")

    with_collection.post("/review/42/deposit", data=COMPLETE)

    with open_log(app.state.settings.submissions_log_path) as history:
        rows = history.for_person(base_url=shipped_base_url(app), eperson_uuid="eperson-1")
    assert len(rows) == 1
    assert rows[0].wsi_id == "42"
    assert rows[0].title == "A Logged Thesis"


def test_a_broken_log_does_not_fail_a_successful_deposit(
    with_collection, app, tmp_path: Path
) -> None:
    """The repository already has the item. Nothing about our own notes may turn
    that into an error page."""
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database")
    app.state.settings.submissions_log_path = broken

    response = with_collection.post("/review/42/deposit", data=COMPLETE)

    assert response.status_code == 200
    assert "Submitted for review" in response.text or "Deposited" in response.text
