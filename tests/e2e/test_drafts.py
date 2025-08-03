"""The drafts list in a browser: readiness badges and the two-step discard.

Discarding swaps a confirmation over the row's own actions and then replaces the
whole list — two different targets in one interaction, which is exactly the sort
of thing that reads fine in the template and lands in the wrong place live.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def given_drafts(fake_dspace, *items) -> None:
    fake_dspace.workspaceitems = {item.id: item for item in items}


def test_each_row_shows_what_its_draft_still_needs(signed_in_page, live_app, fake_dspace) -> None:
    from tests.fakes import make_workspaceitem

    given_drafts(
        fake_dspace,
        make_workspaceitem("7"),  # the fake's default: three required fields
        make_workspaceitem("8", errors=[]),
    )
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")

    assert "Needs 3" in page.inner_text("#draft-7")
    assert "Title — This field is required." in page.inner_text("#draft-7")
    assert "Ready" in page.inner_text("#draft-8")
    assert "Review & submit" in page.inner_text("#draft-8")


def test_discarding_asks_in_the_row_then_replaces_the_list(
    signed_in_page, live_app, fake_dspace
) -> None:
    from tests.fakes import make_workspaceitem

    given_drafts(fake_dspace, make_workspaceitem("7"), make_workspaceitem("8"))
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")

    page.locator("#draft-7 button:has-text('Discard')").click()
    # The confirmation lands over this row's actions, not over the page.
    page.wait_for_selector("#draft-7 .draft-actions:has-text('Discard permanently?')")
    assert "Discard permanently?" not in page.inner_text("#draft-8")

    fake_dspace.workspaceitems.pop("7")
    page.locator("#draft-7 .draft-actions button:has-text('Discard')").click()

    page.wait_for_selector("#drafts-list:not(:has(#draft-7))", timeout=15_000)
    assert page.locator("#draft-8").count() == 1, "only the discarded row goes"


def test_keeping_a_draft_puts_the_list_back(signed_in_page, live_app, fake_dspace) -> None:
    from tests.fakes import make_workspaceitem

    given_drafts(fake_dspace, make_workspaceitem("7"))
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")

    page.locator("#draft-7 button:has-text('Discard')").click()
    page.wait_for_selector("#draft-7 .draft-actions:has-text('Discard permanently?')")
    page.locator("#draft-7 button:has-text('Keep')").click()

    page.wait_for_selector("#draft-7 .draft-actions a:has-text('Resume')")
    assert fake_dspace.deleted == [], "Keep must not delete anything"
