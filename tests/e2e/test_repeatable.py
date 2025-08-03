"""The review form's repeatable rows, and whether edits survive the confirm step.

Two things only a browser can answer. *Add another* is an htmx swap; **Remove** is
`hx-on:click="this.closest('.field-row').remove()"`, which htmx compiles with
`new Function` — so it is also the live test of the `'unsafe-eval'` the CSP grants.
And the review form lives in the page until deposit, so anything lost between
"Review & submit" and "Back to editing" is the user's typing.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def review_page(signed_in_page, live_app, fake_dspace):
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page = signed_in_page
    page.goto(f"{live_app}/review/42")
    page.wait_for_selector("form input[name*='dc.title']")
    return page


AUTHOR_INPUT = "input[name$='dc.contributor.author']"


def authors(page):
    return page.locator(AUTHOR_INPUT)


def _wait_for_authors(page, count: int) -> None:
    page.wait_for_function(f'() => document.querySelectorAll("{AUTHOR_INPUT}").length === {count}')


def test_add_another_appends_a_row_and_remove_takes_it_away(review_page) -> None:
    page = review_page
    before = authors(page).count()

    page.locator(".add-row").first.click()
    _wait_for_authors(page, before + 1)

    # Scoped to *this* field's rows: every repeatable field on the page has
    # `.field-row .row-remove`, so an unscoped `.last` removes a subject.
    #
    # The remove control is an inline handler: if the CSP ever drops
    # 'unsafe-eval', this is the assertion that goes red.
    page.locator(f".field-row:has({AUTHOR_INPUT}) .row-remove").last.click()
    _wait_for_authors(page, before)


def test_typing_survives_the_confirm_step_and_coming_back(review_page, live_app) -> None:
    page = review_page
    page.fill("input[name$='dc.title']", "A Title Typed By Hand")
    page.locator("input[name$='dc.contributor.author']").first.fill("Roe, Richard")

    page.locator("button:has-text('Review & submit')").click()
    page.wait_for_url(f"{live_app}/review/42/confirm", timeout=15_000)
    assert "A Title Typed By Hand" in page.inner_text("body")

    page.locator("button:has-text('Back to editing')").click()
    page.wait_for_selector("input[name$='dc.title']")

    assert page.input_value("input[name$='dc.title']") == "A Title Typed By Hand"
    assert (
        page.locator("input[name$='dc.contributor.author']").first.input_value() == "Roe, Richard"
    )
