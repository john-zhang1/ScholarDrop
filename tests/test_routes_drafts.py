"""Resuming and discarding in-progress submissions (DESIGN.md §6).

Creating the workspaceitem at upload time buys resumability and costs cleanup;
this is the cleanup, and it asks before it deletes.
"""

from __future__ import annotations

from scholardrop.dspace import DSpaceUnavailable
from tests.fakes import make_collection, make_workspaceitem, metadata_values

HTMX = {"HX-Request": "true"}


def given_drafts(fake_dspace, *items) -> None:
    fake_dspace.workspaceitems = {item.id: item for item in items}


def test_the_page_loads_its_list_with_htmx(signed_in) -> None:
    response = signed_in.get("/drafts")

    assert response.status_code == 200
    assert 'hx-get="/drafts/list"' in response.text


def test_drafts_come_from_find_by_submitter_for_this_user(signed_in, fake_dspace) -> None:
    given_drafts(
        fake_dspace,
        make_workspaceitem(
            "7", sections={"traditionalpageone": {"dc.title": metadata_values("Half a thesis")}}
        ),
    )

    response = signed_in.get("/drafts/list")

    assert "Half a thesis" in response.text
    args, _ = fake_dspace.call_args("find_by_submitter")
    assert args[0] == "eperson-1", "DSpace requires the submitter uuid"


def test_a_draft_offers_resume_and_discard(signed_in, fake_dspace) -> None:
    given_drafts(fake_dspace, make_workspaceitem("7"))

    body = signed_in.get("/drafts/list").text

    assert 'href="/review/7"' in body
    assert 'hx-get="/drafts/7/discard"' in body


def test_a_draft_shows_its_collection_and_file(signed_in, fake_dspace) -> None:
    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    given_drafts(
        fake_dspace,
        make_workspaceitem("7", collection_uuid="c1", files=[{"name": "thesis.pdf"}]),
    )

    body = signed_in.get("/drafts/list").text

    assert "OU - Dissertations" in body
    assert "thesis.pdf" in body


def test_an_untitled_draft_is_still_resumable(signed_in, fake_dspace) -> None:
    given_drafts(fake_dspace, make_workspaceitem("7"))

    body = signed_in.get("/drafts/list").text

    assert "Untitled draft" in body
    assert "no file attached" in body


def test_no_drafts_says_so(signed_in) -> None:
    assert "Nothing in progress" in signed_in.get("/drafts/list").text


def test_a_collection_that_cannot_be_read_does_not_break_the_list(signed_in, fake_dspace) -> None:
    given_drafts(fake_dspace, make_workspaceitem("7", collection_uuid="c1"))
    fake_dspace.raise_on["collection"] = DSpaceUnavailable("boom", status=500)

    response = signed_in.get("/drafts/list")

    assert response.status_code == 200
    assert 'href="/review/7"' in response.text


# ---- discarding ------------------------------------------------------------


def test_discard_asks_first(signed_in, fake_dspace) -> None:
    given_drafts(fake_dspace, make_workspaceitem("7"))

    response = signed_in.get("/drafts/7/discard", headers=HTMX)

    assert "Discard permanently?" in response.text
    assert 'hx-post="/drafts/7/discard"' in response.text
    assert not fake_dspace.called("delete_workspaceitem"), "asking must not delete"


def test_confirming_deletes_the_draft_and_returns_the_list(signed_in, fake_dspace) -> None:
    given_drafts(fake_dspace, make_workspaceitem("7"), make_workspaceitem("8"))

    response = signed_in.post("/drafts/7/discard", headers=HTMX)

    assert response.status_code == 200
    assert fake_dspace.deleted == ["7"]
    assert "/review/8" in response.text
    assert "/review/7" not in response.text


def test_discarding_the_current_draft_clears_it_from_the_session(
    with_collection, app, fake_dspace, pdf_bytes
) -> None:
    with_collection.post(
        "/submit", files={"file": ("t.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )
    session = next(iter(app.state.sessions._sessions.values()))
    assert session.workspaceitem_id == "42"

    with_collection.post("/drafts/42/discard", headers=HTMX)

    assert session.workspaceitem_id is None
    assert app.state.extractions.get(session.id, "42") is None


def test_drafts_need_a_session(web) -> None:
    assert web.get("/drafts").status_code == 303
    assert web.post("/drafts/7/discard").status_code == 303


# ---- readiness (Phase 4b) --------------------------------------------------


def test_a_row_says_what_its_draft_still_needs(signed_in, fake_dspace) -> None:
    """The whole point: no opening a draft to find out it is one field short."""
    given_drafts(fake_dspace, make_workspaceitem("7"))  # the fake's default: 3 required fields

    body = signed_in.get("/drafts/list").text

    assert "Needs 3" in body
    # Labelled as the collection labels them, not as DSpace names them.
    assert "Title — This field is required." in body
    assert "dc.title" not in body


def test_a_complete_draft_says_so_and_offers_the_submit_step(signed_in, fake_dspace) -> None:
    given_drafts(fake_dspace, make_workspaceitem("7", errors=[]))

    body = signed_in.get("/drafts/list").text

    assert "Ready" in body
    assert "Review &amp; submit" in body
    assert "1 ready to deposit on this page" in body


def test_an_unreadable_form_config_degrades_the_wording_not_the_list(
    signed_in, fake_dspace
) -> None:
    """A draft that cannot be labelled still gets to say what is wrong with it."""
    fake_dspace.raise_on["submission_definition"] = DSpaceUnavailable("config down", status=503)
    given_drafts(fake_dspace, make_workspaceitem("7"))

    response = signed_in.get("/drafts/list")

    assert response.status_code == 200
    assert "Needs 3" in response.text
    assert "dc.title" in response.text, "the repository's own name beats silence"
    assert "form configuration could not be read" in response.text


def test_the_form_config_is_read_once_for_a_page_of_drafts(signed_in, fake_dspace) -> None:
    """Ten drafts in one collection are one definition, not ten."""
    given_drafts(fake_dspace, *[make_workspaceitem(str(i)) for i in range(4)])

    signed_in.get("/drafts/list")

    definitions = [c for c in fake_dspace.calls if c[0] == "submission_definition"]
    assert len(definitions) == 1
