"""The collection browser: tabs, live search, and the lazily-loaded tree.

`<details>` + `hx-trigger="toggle from:closest details once"` is how a community
loads its children, and `once` is the whole reason a user can open and close a node
without hammering the repository. Nothing but a browser can check that.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def given_a_tree(fake_dspace) -> None:
    from tests.fakes import make_collection, make_community

    fake_dspace.communities = [make_community("com1", "University of Oklahoma")]
    fake_dspace.subcommunity_map["com1"] = [make_community("com2", "OU Libraries")]
    fake_dspace.collection_map["com1"] = [make_collection("c1", "OU - Dissertations")]


def test_the_tabs_swap_the_panel_without_navigating(signed_in_page, live_app, fake_dspace) -> None:
    """The tab buttons carry `hx-on:click`, which needs the CSP to allow eval."""
    given_a_tree(fake_dspace)
    page = signed_in_page
    page.goto(f"{live_app}/browse")
    page.wait_for_selector("#tab-panel .collection-list, #tab-panel p")

    page.click("button[role=tab]:has-text('Browse all')")

    page.wait_for_selector("#tab-panel details")
    assert page.url.endswith("/browse"), "a tab is a swap, not a page load"
    # `hx-on:click` moved the active class -- proof the inline handler compiled.
    assert "is-active" in (
        page.get_attribute("button[role=tab]:has-text('Browse all')", "class") or ""
    )


def test_a_community_loads_its_children_once_however_often_it_is_toggled(
    signed_in_page, live_app, fake_dspace
) -> None:
    given_a_tree(fake_dspace)
    page = signed_in_page
    page.goto(f"{live_app}/browse?tab=all")
    node = page.wait_for_selector("details")

    node.click()  # open
    page.wait_for_selector("details .collection-name, details details")
    node.click()  # close
    node.click()  # open again

    page.wait_for_timeout(300)  # let any stray request arrive before counting
    loads = [c for c in fake_dspace.calls if c[0] in {"subcommunities", "collections_of"}]
    assert len(loads) == 2, f"one subcommunities + one collections call, got {loads}"


def test_live_search_filters_without_reloading(signed_in_page, live_app, fake_dspace) -> None:
    from tests.fakes import make_collection

    fake_dspace.authorized_collections = [
        make_collection("c1", "OU - Dissertations"),
        make_collection("c2", "OU - Theses"),
    ]
    page = signed_in_page
    page.goto(f"{live_app}/browse")
    page.wait_for_selector("#mine-results .collection-name")

    fake_dspace.authorized_collections = [make_collection("c2", "OU - Theses")]
    # Typed, not filled: the search is `hx-trigger="keyup changed delay:300ms"`,
    # and `fill()` dispatches `input` only, so it would never fire.
    page.locator("input[name=q]").press_sequentially("theses", delay=20)
    page.wait_for_function(
        "() => document.querySelectorAll('#mine-results .collection-name').length === 1",
        timeout=10_000,
    )

    assert "OU - Theses" in page.inner_text("#mine-results")
    # The *last* search, not the first: the panel's own `hx-trigger="load"` fetches
    # the unfiltered list before anyone types.
    searches = [c for c in fake_dspace.calls if c[0] == "find_submit_authorized"]
    _name, args, kwargs = searches[-1]
    assert (args and args[0] == "theses") or kwargs.get("query") == "theses"
