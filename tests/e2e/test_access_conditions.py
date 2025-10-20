"""The access picker in a real browser (DESIGN.md §13.5).

Two things no route test can see: the `change` trigger that fetches the date box
for the condition just picked, and the fact that choosing a condition which takes
no date *clears* one that was already filled in. The second is the one that
matters — a stale date left in the DOM would be posted, and DSpace refuses an
open-access condition that carries a date.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

PICKER = "select[name='access::0']"
DATE = "input[name='access::0::date']"
NO_DATE_BOX = f'document.querySelectorAll("{DATE}").length === 0'


@pytest.fixture
def review_page(signed_in_page, live_app, fake_dspace):
    from tests.fakes import make_collection, make_workspaceitem, upload_file

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    fake_dspace.workspaceitems["42"] = make_workspaceitem("42", files=[upload_file("thesis.pdf")])
    page = signed_in_page
    page.goto(f"{live_app}/review/42")
    page.wait_for_selector(PICKER)
    return page


def test_the_picker_starts_with_no_date_box(review_page) -> None:
    assert review_page.locator(DATE).count() == 0


def test_choosing_an_embargo_asks_for_the_date_it_needs(review_page) -> None:
    review_page.locator(PICKER).select_option("embargo")

    review_page.wait_for_selector(DATE, timeout=15_000)
    assert "Restricted until" in review_page.inner_text(".access-choice")
    assert review_page.locator(DATE).get_attribute("max") == "2029-07-30"


def test_choosing_open_access_takes_the_date_box_away(review_page) -> None:
    """DSpace refuses a condition that takes no date but is given one."""
    page = review_page
    page.locator(PICKER).select_option("embargo")
    page.wait_for_selector(DATE, timeout=15_000)
    page.locator(DATE).fill("2028-01-01")

    page.locator(PICKER).select_option("openaccess")

    page.wait_for_function(NO_DATE_BOX, timeout=15_000)


def test_the_choice_reaches_the_confirm_step(review_page) -> None:
    page = review_page
    page.locator(PICKER).select_option("embargo")
    page.wait_for_selector(DATE, timeout=15_000)
    page.locator(DATE).fill("2028-01-01")

    page.click("form.submission-form button[type=submit]")
    page.wait_for_selector("form[action$='/deposit']", timeout=15_000)

    assert "restricted until 2028-01-01" in page.inner_text("body")
    hidden = page.locator("form[action$='/deposit'] input[name='access::0::date']")
    assert hidden.input_value() == "2028-01-01"
