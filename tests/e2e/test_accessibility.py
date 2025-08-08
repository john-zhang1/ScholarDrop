"""Keyboard, names, states and focus — the half of the gate a browser test can do.

This is not a substitute for someone using the app with a screen reader; it is the
set of things that are objectively checkable and that regressed silently before
anyone noticed. Contrast is checked in `tests/test_accessibility_contrast.py`,
where it needs no browser at all.

The accessible-name computation used here is deliberately simple — `aria-label`,
`aria-labelledby`, a wrapping or `for=` label, a title — rather than a full
implementation. It is enough to catch the failure that mattered: an input with no
name at all.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# Chromium's own accessible-name rules are not exposed to page scripts, so this
# covers the cases the app actually produces.
ACCESSIBLE_NAME = """
(el) => {
  const byLabel = el.getAttribute('aria-label');
  if (byLabel && byLabel.trim()) return byLabel.trim();
  const ref = el.getAttribute('aria-labelledby');
  if (ref) {
    const target = document.getElementById(ref.split(/\\s+/)[0]);
    if (target && target.textContent.trim()) return target.textContent.trim();
  }
  if (el.id) {
    const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (label && label.textContent.trim()) return label.textContent.trim();
  }
  const wrapping = el.closest('label');
  if (wrapping && wrapping.textContent.trim()) return wrapping.textContent.trim();
  const title = el.getAttribute('title');
  return title && title.trim() ? title.trim() : '';
}
"""


def unnamed_controls(page) -> list[str]:
    """Every visible control that would be announced with no name."""
    return page.eval_on_selector_all(
        "input:not([type=hidden]), select, textarea, button, a[href]",
        """(nodes, nameOf) => {
            const named = eval(`(${nameOf})`);
            return nodes
              .filter(el => el.offsetParent !== null || el.getClientRects().length)
              .filter(el => !named(el) && !el.textContent.trim())
              .map(el => el.outerHTML.slice(0, 120));
        }""",
        ACCESSIBLE_NAME,
    )


@pytest.fixture
def review_page(signed_in_page, live_app, fake_dspace):
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page = signed_in_page
    page.goto(f"{live_app}/review/42")
    page.wait_for_selector("form input[name*='dc.title']")
    return page


# ---- names -----------------------------------------------------------------


def test_every_control_on_the_review_form_has_a_name(review_page) -> None:
    assert unnamed_controls(review_page) == []


def test_an_added_row_is_named_too(review_page) -> None:
    """The bug this pass found: only the first row carried the label's id."""
    page = review_page
    before = page.locator("input[name$='dc.contributor.author']").count()
    page.locator(".add-row").first.click()
    page.wait_for_function(
        f"() => document.querySelectorAll(\"input[name$='dc.contributor.author']\").length"
        f" === {before + 1}"
    )

    assert unnamed_controls(page) == []
    added = page.locator("input[name$='dc.contributor.author']").last
    assert "Author" in (added.get_attribute("aria-label") or "")


def test_the_drafts_and_browse_pages_name_their_controls(
    signed_in_page, live_app, fake_dspace
) -> None:
    from tests.fakes import make_collection, make_community, make_workspaceitem

    fake_dspace.workspaceitems = {"7": make_workspaceitem("7")}
    fake_dspace.communities = [make_community("com1", "University of Oklahoma")]
    fake_dspace.authorized_collections = [make_collection("c1", "OU - Dissertations")]
    page = signed_in_page

    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")
    assert unnamed_controls(page) == []

    page.goto(f"{live_app}/browse")
    page.wait_for_selector("#tab-panel .collection-name, #tab-panel p")
    assert unnamed_controls(page) == []


# ---- state -----------------------------------------------------------------


def test_required_and_invalid_fields_say_so_in_the_accessibility_tree(review_page) -> None:
    """`required` in red text is not a state a screen reader can read."""
    page = review_page
    title = page.locator("input[name$='dc.title']").first

    assert title.get_attribute("aria-required") == "true"
    # The fake's draft is missing its required fields, so the form is complaining.
    assert title.get_attribute("aria-invalid") == "true"
    described = title.get_attribute("aria-describedby") or ""
    assert described, "the error text must be tied to the input"
    for token in described.split():
        assert page.locator(f"#{token}").count() == 1, f"{token} is referenced but not rendered"


def test_a_native_required_attribute_is_not_used(review_page) -> None:
    """DSpace decides what is complete; the browser must not block a draft."""
    assert review_page.locator("form [required]").count() == 0


def test_the_browse_tabs_report_which_one_is_pressed(signed_in_page, live_app, fake_dspace) -> None:
    from tests.fakes import make_community

    fake_dspace.communities = [make_community("com1", "University of Oklahoma")]
    page = signed_in_page
    page.goto(f"{live_app}/browse")
    page.wait_for_selector("#tab-panel")

    mine = page.locator("button.tab", has_text="My collections")
    everything = page.locator("button.tab", has_text="Browse all")
    assert mine.get_attribute("aria-pressed") == "true"
    assert everything.get_attribute("aria-pressed") == "false"

    everything.click()
    page.wait_for_selector("#tab-panel details")

    assert everything.get_attribute("aria-pressed") == "true"
    assert mine.get_attribute("aria-pressed") == "false"


def test_no_element_claims_a_tab_role_without_the_keyboard_pattern(
    signed_in_page, live_app
) -> None:
    """role=tab promises arrow-key navigation this app does not implement."""
    page = signed_in_page
    page.goto(f"{live_app}/browse")
    page.wait_for_selector("#tab-panel")

    assert page.locator("[role=tab], [role=tablist], [role=tabpanel]").count() == 0


# ---- keyboard and focus ----------------------------------------------------


def test_the_skip_link_is_first_and_moves_focus_to_the_content(signed_in_page, live_app) -> None:
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.keyboard.press("Tab")

    assert page.evaluate("document.activeElement.className") == "skip-link"
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.activeElement.id === 'content'")


def test_the_discard_confirmation_takes_focus(signed_in_page, live_app, fake_dspace) -> None:
    """A destructive choice appearing away from the keyboard is made blind."""
    from tests.fakes import make_workspaceitem

    fake_dspace.workspaceitems = {"7": make_workspaceitem("7")}
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")

    page.locator("#draft-7 button:has-text('Discard')").click()
    page.wait_for_selector("#draft-7 .draft-actions .danger")

    page.wait_for_function(
        "() => document.activeElement.classList.contains('danger')",
        timeout=5_000,
    )


def test_keeping_a_draft_restores_the_row_without_refetching_the_list(
    signed_in_page, live_app, fake_dspace
) -> None:
    """Backing out must not re-read every draft's form configuration."""
    from tests.fakes import make_workspaceitem

    fake_dspace.workspaceitems = {"7": make_workspaceitem("7"), "8": make_workspaceitem("8")}
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")
    fake_dspace.calls.clear()

    page.locator("#draft-7 button:has-text('Discard')").click()
    page.wait_for_selector("#draft-7 .draft-actions .danger")
    page.locator("#draft-7 button:has-text('Keep')").click()
    page.wait_for_selector("#draft-7 .draft-actions a:has-text('Resume')")

    assert not any(c[0] == "find_by_submitter" for c in fake_dspace.calls), (
        "declining to discard must not reload the list"
    )
    assert page.locator("#draft-8").count() == 1


def test_nothing_takes_itself_out_of_the_natural_tab_order(
    signed_in_page, live_app, fake_dspace
) -> None:
    """A positive tabindex reorders the page for keyboard users only."""
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page = signed_in_page
    for path in ("/drafts", "/browse", "/review/42"):
        page.goto(f"{live_app}{path}")
        page.wait_for_load_state("domcontentloaded")
        positive = page.eval_on_selector_all(
            "[tabindex]",
            "nodes => nodes.filter(n => Number(n.getAttribute('tabindex')) > 0).length",
        )
        assert positive == 0, f"{path} has a positive tabindex"


# ---- structure -------------------------------------------------------------


def test_each_page_has_one_h1_and_a_main_landmark(signed_in_page, live_app) -> None:
    page = signed_in_page
    for path in ("/drafts", "/browse", "/submit", "/review/42"):
        page.goto(f"{live_app}{path}")
        page.wait_for_load_state("domcontentloaded")
        assert page.locator("h1").count() == 1, f"{path} should have exactly one h1"
        assert page.locator("main#content").count() == 1, f"{path} has no main landmark"


def test_ids_stay_unique_as_rows_are_added(review_page) -> None:
    """A repeated id is how the added row silently lost its label."""
    page = review_page

    def duplicates() -> list[str]:
        return page.evaluate("""() => {
            const seen = {}, dupes = [];
            for (const el of document.querySelectorAll('[id]')) {
              if (seen[el.id]) dupes.push(el.id); else seen[el.id] = true;
            }
            return dupes;
        }""")

    assert duplicates() == []
    page.locator(".add-row").first.click()
    page.wait_for_timeout(300)
    page.locator(".add-row").first.click()
    page.wait_for_timeout(300)
    assert duplicates() == []


def test_the_progress_state_is_announced(
    signed_in_page, live_app, fake_dspace, fake_extractor, pdf_file
) -> None:
    """Extraction runs in the background; a silent region is a silent wait."""
    import asyncio

    from tests.fakes import make_collection

    fake_extractor.gate = asyncio.Event()
    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page = signed_in_page
    page.goto(f"{live_app}/select/c1?verified=1")
    page.wait_for_url(f"{live_app}/submit**")

    page.set_input_files("input[type=file]", str(pdf_file))
    page.locator(".dropzone button[type=submit]").click()
    progress = page.wait_for_selector("#upload-panel .progress")

    assert progress.get_attribute("role") == "status"
    assert progress.get_attribute("aria-live") == "polite"
