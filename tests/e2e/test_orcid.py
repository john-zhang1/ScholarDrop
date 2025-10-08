"""The ORCID type-ahead, in a real browser (DESIGN.md §13.2).

Three mechanisms here exist only in the browser and no route test can see them:
the debounced `keyup` trigger, `hx-vals="js:{q: event.target.value}"` (which is
how the typed text reaches an endpoint that cannot know the input's name), and
the `hx-on:click` that fills the name box and the hidden iD beside it. Each has a
silent failure mode -- a request that never fires, one that fires with no query,
or a click that changes nothing -- and each would leave the route tests green.

ORCID itself is stubbed: this proves ScholarDrop's behaviour, never ORCID's.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

ORCID = "0000-0002-1825-0097"
AUTHOR_INPUT = "input[name$='dc.contributor.author']"


@pytest.fixture
def stub_orcid(monkeypatch):
    """Whatever is typed, one match -- and a record of what was asked."""
    from scholardrop import orcid

    asked: list[str] = []

    async def fake_search(typed, settings, **kwargs):
        asked.append(typed)
        if len(typed.strip()) < settings.orcid_min_query_length:
            return []
        return [
            orcid.OrcidPerson(
                orcid_id=ORCID,
                given="Josiah",
                family="Carberry",
                institutions=("Brown University",),
            )
        ]

    monkeypatch.setattr(orcid, "search", fake_search)
    return asked


@pytest.fixture
def review_page(signed_in_page, live_app, fake_dspace):
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page = signed_in_page
    page.goto(f"{live_app}/review/42")
    page.wait_for_selector(AUTHOR_INPUT)
    return page


def test_typing_a_name_offers_matching_people(review_page, stub_orcid) -> None:
    page = review_page

    # press_sequentially, not fill(): the trigger is `keyup`, and fill() fires
    # `input` only -- the same trap the live collection search set.
    page.locator(AUTHOR_INPUT).first.press_sequentially("Carberry")

    suggestion = page.wait_for_selector(".orcid-choice", timeout=15_000)
    assert "Carberry, Josiah" in suggestion.inner_text()
    assert "Brown University" in suggestion.inner_text()
    # The typed text reached the endpoint -- hx-vals did its job.
    assert stub_orcid and stub_orcid[-1].startswith("Carberry")


def test_choosing_a_match_fills_the_name_and_keeps_the_id(review_page, stub_orcid) -> None:
    page = review_page
    page.locator(AUTHOR_INPUT).first.press_sequentially("Carberry")
    page.wait_for_selector(".orcid-choice", timeout=15_000)

    page.locator(".orcid-choice").first.click()

    assert page.locator(AUTHOR_INPUT).first.input_value() == "Carberry, Josiah"
    assert page.locator("input.orcid-input").first.input_value() == ORCID
    # And the suggestion list gets out of the way once it has been used.
    assert page.locator(".orcid-choice").count() == 0


def test_the_chosen_id_is_posted_with_the_form(review_page, stub_orcid, live_app) -> None:
    """The iD has to survive the step between the form and the deposit."""
    page = review_page
    page.locator(AUTHOR_INPUT).first.press_sequentially("Carberry")
    page.wait_for_selector(".orcid-choice", timeout=15_000)
    page.locator(".orcid-choice").first.click()

    page.click("form.submission-form button[type=submit]")
    page.wait_for_selector("form[action$='/deposit']", timeout=15_000)

    hidden = page.locator("form[action$='/deposit'] input[name$='::orcid']").first
    assert hidden.input_value() == ORCID


def test_two_characters_do_not_ask_anybody(review_page, stub_orcid) -> None:
    page = review_page
    page.locator(AUTHOR_INPUT).first.press_sequentially("Ca")
    page.wait_for_timeout(1200)

    assert page.locator(".orcid-choice").count() == 0
