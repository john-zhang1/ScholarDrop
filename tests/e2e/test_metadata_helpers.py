"""The Phase 11 helpers, in a real browser (DESIGN.md §13.4).

Three mechanisms here live only in the browser, and a route test sees none of
them: the debounced `keyup` that searches a controlled vocabulary, the
`hx-on:click` that writes a chosen term into the box, and the "Add a
translation" button that appends a row to a field DSpace calls single-valued.
Each fails silently -- a request that never fires, a click that changes nothing,
a row that never arrives -- and each would leave the fast suite green.

DSpace is the same fake as everywhere else: this proves ScholarDrop's behaviour,
never the repository's.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

TYPE_INPUT = "input[name$='dc.type']"
TITLE_ROWS = "#rows-traditionalpageone--dc-title .field-row"
TWO_ROWS = f"document.querySelectorAll('{TITLE_ROWS}').length === 2"


@pytest.fixture
def review_page(signed_in_page, live_app, fake_dspace):
    from scholardrop.dspace import VocabularyEntry
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    # The state a real repository is in: the vocabulary exists but will not be
    # listed, so the field is a search box rather than a <select>.
    fake_dspace.vocabularies = {}
    fake_dspace.searchable_vocabularies["common_types"] = [
        VocabularyEntry(value="Thesis", display="Thesis"),
        VocabularyEntry(value="Technical Report", display="Technical Report"),
    ]
    page = signed_in_page
    page.goto(f"{live_app}/review/42")
    page.wait_for_selector(TYPE_INPUT)
    return page


def test_typing_searches_the_vocabulary(review_page) -> None:
    # press_sequentially, not fill(): the trigger is `keyup`.
    review_page.locator(TYPE_INPUT).first.press_sequentially("thes")

    suggestion = review_page.wait_for_selector(".vocabulary-choice", timeout=15_000)
    assert "Thesis" in suggestion.inner_text()


def test_choosing_a_term_fills_the_box(review_page) -> None:
    page = review_page
    page.locator(TYPE_INPUT).first.press_sequentially("thes")
    page.wait_for_selector(".vocabulary-choice", timeout=15_000)

    page.locator(".vocabulary-choice").first.click()

    assert page.locator(TYPE_INPUT).first.input_value() == "Thesis"
    # And the list gets out of the way once it has been used.
    assert page.locator(".vocabulary-choice").count() == 0


def test_one_character_asks_nobody(review_page) -> None:
    review_page.locator(TYPE_INPUT).first.press_sequentially("t")
    review_page.wait_for_timeout(1200)

    assert review_page.locator(".vocabulary-choice").count() == 0


def test_adding_a_translation_appends_a_row_with_its_own_language(review_page) -> None:
    """dc.title is configured single-valued; a translation is the row it accepts."""
    page = review_page
    assert page.locator(TITLE_ROWS).count() == 1

    page.click(".add-translation")
    page.wait_for_function(TWO_ROWS, timeout=15_000)

    languages = page.locator(f"{TITLE_ROWS} select.row-language")
    assert languages.count() == 2
    # The appended row is empty and its language unset, waiting to be told both.
    assert languages.nth(1).input_value() == ""


def test_a_translation_survives_the_confirm_step(review_page) -> None:
    page = review_page
    page.locator(f"{TITLE_ROWS} input").first.fill("A Study of Sediment")
    page.locator(f"{TITLE_ROWS} select.row-language").first.select_option("en")
    page.click(".add-translation")
    page.wait_for_function(TWO_ROWS, timeout=15_000)
    page.locator(f"{TITLE_ROWS} input").nth(1).fill("泥沙运移研究")
    page.locator(f"{TITLE_ROWS} select.row-language").nth(1).select_option("zh")

    page.click("form.submission-form button[type=submit]")
    page.wait_for_selector("form[action$='/deposit']", timeout=15_000)

    hidden = page.locator("form[action$='/deposit'] input[name$='::lang']")
    assert [hidden.nth(i).input_value() for i in range(hidden.count())] == ["en", "zh"]
