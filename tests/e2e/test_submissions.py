"""The status page in a browser (DESIGN.md §13.3).

Two things a route test cannot see: that the htmx swap actually lands (the list
is loaded, not rendered with the page), and that the draft actions borrowed from
the drafts list still work when they are on a *different* page — their targets
are ids, and an id that does not exist here would fail silently.
"""

from __future__ import annotations

import pytest

from tests.conftest import SHIPPED_INSTANCE_KEY, shipped_base_url

pytestmark = pytest.mark.e2e


def given_deposit(app, **overrides: object) -> None:
    from scholardrop.history.log import Deposit, open_log

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
    with open_log(app.state.settings.submissions_log_path) as history:
        assert history.record(Deposit(**fields))  # type: ignore[arg-type]


def test_the_list_loads_and_shows_each_state(signed_in_page, live_app, app, fake_dspace) -> None:
    from scholardrop.dspace.models import ItemStatus
    from tests.fakes import make_workspaceitem, metadata_values

    given_deposit(app, wsi_id="1", item_uuid="item-live", title="The published one")
    given_deposit(app, wsi_id="2", item_uuid="item-review", title="The one being reviewed")
    fake_dspace.item_statuses = {
        "item-live": ItemStatus(uuid="item-live", found=True, in_archive=True, handle="11244/1"),
        "item-review": ItemStatus(uuid="item-review", found=True, in_workflow=True),
    }
    fake_dspace.workspaceitems = {
        "7": make_workspaceitem(
            "7",
            sections={"traditionalpageone": {"dc.title": metadata_values("The unfinished one")}},
        )
    }

    page = signed_in_page
    page.goto(f"{live_app}/my-submissions")
    page.wait_for_selector("li.submission")

    body = page.inner_text("#submissions-list")
    assert "The published one" in body
    assert "The one being reviewed" in body
    assert "The unfinished one" in body
    assert "Live" in body and "In review" in body and "Draft" in body


def test_a_draft_can_still_be_discarded_from_here(
    signed_in_page, live_app, app, fake_dspace
) -> None:
    """The actions partial targets `#draft-{id}`, which this page has to render
    too — otherwise the click swaps into nothing and looks like a dead button."""
    from tests.fakes import make_workspaceitem

    fake_dspace.workspaceitems = {"7": make_workspaceitem("7")}
    page = signed_in_page
    page.goto(f"{live_app}/my-submissions")
    page.wait_for_selector("li.submission")

    page.locator("li.submission button:has-text('Discard')").click()
    page.wait_for_selector("text=Discard permanently?")


def test_a_live_item_links_to_its_handle(signed_in_page, live_app, app, fake_dspace) -> None:
    from scholardrop.dspace.models import ItemStatus

    given_deposit(app, item_uuid="item-1")
    fake_dspace.item_statuses["item-1"] = ItemStatus(
        uuid="item-1", found=True, in_archive=True, handle="11244/345333"
    )

    page = signed_in_page
    page.goto(f"{live_app}/my-submissions")
    page.wait_for_selector("li.submission")

    link = page.locator("a[href='https://hdl.handle.net/11244/345333']").first
    assert link.count() >= 1


def test_the_page_is_reachable_from_the_header(signed_in_page, live_app) -> None:
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.click("header a:has-text('Submissions')")
    page.wait_for_url(f"{live_app}/my-submissions")


def test_an_unreadable_row_says_so_beside_the_ones_that_read(
    signed_in_page, live_app, app, fake_dspace
) -> None:
    from scholardrop.dspace import DSpaceUnavailable

    given_deposit(app, wsi_id="1", item_uuid="item-1", title="Cannot be checked")
    fake_dspace.raise_on["item_status"] = DSpaceUnavailable("down", path="/core/items")

    page = signed_in_page
    page.goto(f"{live_app}/my-submissions")
    page.wait_for_selector("li.submission")

    body = page.inner_text("#submissions-list")
    assert "Cannot be checked" in body
    assert "could not be checked just now" in body
