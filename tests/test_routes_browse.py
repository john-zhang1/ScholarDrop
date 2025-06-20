"""Collection browsing and the ACL gate (DESIGN.md §6 steps 3-4, §4)."""

from __future__ import annotations

from scholardrop.dspace import DSpaceUnavailable
from tests.fakes import make_collection, make_community


def test_browse_renders_both_tabs(signed_in) -> None:
    response = signed_in.get("/browse")

    assert response.status_code == 200
    assert "My collections" in response.text
    assert "Browse all" in response.text
    # The panel is loaded by HTMX, not rendered inline.
    assert 'hx-get="/browse/mine"' in response.text


def test_browse_tab_parameter_selects_the_initial_panel(signed_in) -> None:
    assert 'hx-get="/browse/all"' in signed_in.get("/browse?tab=all").text
    assert 'hx-get="/browse/mine"' in signed_in.get("/browse?tab=mine").text


# ---- My collections --------------------------------------------------------


def test_mine_panel_has_a_debounced_search_box(signed_in) -> None:
    response = signed_in.get("/browse/mine")

    assert response.status_code == 200
    assert 'hx-trigger="keyup changed delay:300ms, search"' in response.text
    assert 'hx-get="/browse/mine/results"' in response.text
    assert 'hx-target="#mine-results"' in response.text


def test_mine_results_come_from_find_submit_authorized(signed_in, fake_dspace) -> None:
    fake_dspace.authorized_collections = [
        make_collection("c1", "OU - Dissertations", "11244/10476"),
        make_collection("c2", "OU - Faculty and Staff Publications", "11244/7920"),
    ]

    response = signed_in.get("/browse/mine/results")

    assert response.status_code == 200
    assert "OU - Dissertations" in response.text
    assert "11244/10476" in response.text
    assert fake_dspace.called("find_submit_authorized")


def test_mine_results_links_skip_the_gate(signed_in, fake_dspace) -> None:
    """Entries here were already filtered by DSpace, so verified=1 is honest."""
    fake_dspace.authorized_collections = [make_collection("c1", "OU - Dissertations")]

    response = signed_in.get("/browse/mine/results")

    assert "/select/c1?verified=1" in response.text


def test_mine_search_query_is_passed_to_dspace(signed_in, fake_dspace) -> None:
    fake_dspace.authorized_collections = [
        make_collection("c1", "OU - Dissertations"),
        make_collection("c2", "Faculty Publications"),
    ]

    response = signed_in.get("/browse/mine/results?q=dissert")

    _, kwargs = fake_dspace.call_args("find_submit_authorized")
    assert kwargs["query"] == "dissert"
    assert "OU - Dissertations" in response.text
    assert "Faculty Publications" not in response.text


def test_mine_search_with_no_matches_says_so(signed_in, fake_dspace) -> None:
    fake_dspace.authorized_collections = [make_collection("c1", "OU - Dissertations")]

    response = signed_in.get("/browse/mine/results?q=zzzz")

    assert "Nothing matches" in response.text


def test_mine_with_no_authorized_collections_points_at_the_contact(signed_in) -> None:
    response = signed_in.get("/browse/mine/results")

    assert "does not list any collections" in response.text
    # contact_email from config/instances.yaml
    assert "shareok@ou.edu" in response.text
    assert "Browse all" in response.text


def test_mine_results_paginate(signed_in, fake_dspace) -> None:
    fake_dspace.authorized_collections = [
        make_collection(f"c{i}", f"Collection {i}") for i in range(25)
    ]

    first = signed_in.get("/browse/mine/results")
    assert "Next page" in first.text
    assert "Previous page" not in first.text

    second = signed_in.get("/browse/mine/results?page=1")
    _, kwargs = fake_dspace.call_args("find_submit_authorized")
    assert kwargs["page"] == 0  # first recorded call
    assert "Previous page" in second.text


def test_negative_page_is_rejected(signed_in) -> None:
    assert signed_in.get("/browse/mine/results?page=-1").status_code == 422


# ---- Browse all ------------------------------------------------------------


def test_all_panel_lists_top_communities_lazily(signed_in, fake_dspace) -> None:
    fake_dspace.communities = [make_community("comm-1", "The University of Oklahoma")]

    response = signed_in.get("/browse/all")

    assert response.status_code == 200
    assert "The University of Oklahoma" in response.text
    assert fake_dspace.called("top_communities")
    # Children are not fetched until the node is expanded.
    assert not fake_dspace.called("subcommunities")
    assert 'hx-get="/browse/node/comm-1?depth=0"' in response.text
    assert 'hx-trigger="toggle from:closest details once"' in response.text


def test_all_panel_uses_native_disclosure(signed_in, fake_dspace) -> None:
    """<details>/<summary> means expanding works without custom JavaScript."""
    fake_dspace.communities = [make_community("comm-1", "OU")]

    response = signed_in.get("/browse/all")

    assert "<details>" in response.text
    assert "<summary>" in response.text


def test_node_returns_subcommunities_and_collections(signed_in, fake_dspace) -> None:
    fake_dspace.subcommunity_map["comm-1"] = [make_community("comm-2", "OU - Health Sciences")]
    fake_dspace.collection_map["comm-1"] = [make_collection("c1", "OU - Dissertations")]

    response = signed_in.get("/browse/node/comm-1")

    assert response.status_code == 200
    assert "OU - Health Sciences" in response.text
    assert "OU - Dissertations" in response.text
    # A nested community is itself lazily expandable.
    assert 'hx-get="/browse/node/comm-2' in response.text


def test_node_collection_links_go_through_the_gate(signed_in, fake_dspace) -> None:
    fake_dspace.collection_map["comm-1"] = [make_collection("c1", "OU - Dissertations")]

    response = signed_in.get("/browse/node/comm-1")

    assert "/select/c1" in response.text
    assert "verified=1" not in response.text, "picks from the full tree must be checked"


def test_empty_node_says_so(signed_in) -> None:
    response = signed_in.get("/browse/node/comm-1")

    assert "Nothing inside this community" in response.text


def test_all_panel_paginates(signed_in, fake_dspace) -> None:
    fake_dspace.communities = [make_community(f"comm-{i}", f"Community {i}") for i in range(25)]

    response = signed_in.get("/browse/all")

    assert "Next page" in response.text
    assert 'hx-get="/browse/all?page=1"' in response.text


# ---- the ACL gate (DESIGN.md §4) -------------------------------------------


def test_select_from_the_tree_runs_can_submit_and_proceeds(signed_in, app, fake_dspace) -> None:
    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    fake_dspace.can_submit_result = True

    response = signed_in.get("/select/c1")

    assert fake_dspace.called("can_submit"), "the gate must run for an unverified pick"
    assert response.status_code == 303
    assert response.headers["location"] == "/submit"
    session = next(iter(app.state.sessions._sessions.values()))
    assert session.collection_uuid == "c1"
    assert session.data["collection_name"] == "OU - Dissertations"


def test_select_denied_shows_the_access_denied_screen(signed_in, app, fake_dspace) -> None:
    fake_dspace.collections_by_uuid["c9"] = make_collection("c9", "OU - Restricted Series")
    fake_dspace.can_submit_result = False

    response = signed_in.get("/select/c9")

    assert response.status_code == 403
    body = response.text
    # Names the collection, who to contact, and a way back (DESIGN.md §4).
    assert "OU - Restricted Series" in body
    assert "shareok@ou.edu" in body
    assert "/browse" in body
    session = next(iter(app.state.sessions._sessions.values()))
    assert session.collection_uuid is None, "a denied pick must not be remembered"


def test_denied_screen_explains_where_rights_live(signed_in, fake_dspace) -> None:
    fake_dspace.can_submit_result = False

    response = signed_in.get("/select/c9")

    assert "live in the repository" in response.text
    assert "My collections" in response.text


def test_select_verified_skips_the_redundant_check(signed_in, fake_dspace) -> None:
    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")

    response = signed_in.get("/select/c1?verified=1")

    assert not fake_dspace.called("can_submit"), "findSubmitAuthorized already filtered"
    assert response.status_code == 303
    assert response.headers["location"] == "/submit"


def test_repository_outage_during_select_is_a_502_page(signed_in, fake_dspace) -> None:
    fake_dspace.raise_on["collection"] = DSpaceUnavailable("connection refused")

    response = signed_in.get("/select/c1")

    assert response.status_code == 502
    assert "not responding" in response.text


# ---- the placeholder next step --------------------------------------------


def test_submit_shows_the_chosen_collection(signed_in, fake_dspace) -> None:
    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations", "11244/1")
    signed_in.get("/select/c1?verified=1")

    response = signed_in.get("/submit")

    assert response.status_code == 200
    assert "OU - Dissertations" in response.text


def test_submit_without_a_collection_goes_back_to_browse(signed_in) -> None:
    response = signed_in.get("/submit")

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
