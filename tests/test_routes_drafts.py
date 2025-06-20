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
