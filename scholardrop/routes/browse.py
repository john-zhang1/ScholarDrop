"""Pick a collection (DESIGN.md §6 steps 3-4).

Two tabs. *My collections* is populated by `findSubmitAuthorized`, which DSpace
filters to what this user may actually submit to -- so a user who just wants to
deposit never sees a collection they cannot use. *Browse all* shows the whole
public tree, and a pick from there goes through the `canSubmit` gate.

That gate is UX, not security: the wall is DSpace itself, which rejects
`create_workspaceitem` for an unauthorized collection no matter what the UI showed
(DESIGN.md §4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from scholardrop.routes.deps import dspace_client, render, require_session

router = APIRouter()
log = logging.getLogger("scholardrop.browse")

PAGE_SIZE = 20


@router.get("/browse", response_class=HTMLResponse)
async def browse(request: Request, tab: str = "mine") -> HTMLResponse:
    require_session(request)
    return render(request, "browse.html", tab="all" if tab == "all" else "mine")


@router.get("/browse/mine", response_class=HTMLResponse)
async def mine_panel(request: Request, q: str = "") -> HTMLResponse:
    """The tab body: a search box plus the first page of results."""
    require_session(request)
    return render(request, "partials/mine.html", q=q)


@router.get("/browse/mine/results", response_class=HTMLResponse)
async def mine_results(request: Request, q: str = "", page: int = Query(0, ge=0)) -> HTMLResponse:
    """Search results only -- swapped separately so the input keeps focus."""
    session = require_session(request)
    async with dspace_client(request, session) as client:
        results = await client.find_submit_authorized(query=q or None, page=page, size=PAGE_SIZE)
    return render(
        request,
        "partials/mine_results.html",
        collections=results.items,
        total=results.total,
        q=q,
        page=page,
        has_next=results.has_next,
    )


@router.get("/browse/all", response_class=HTMLResponse)
async def all_panel(request: Request, page: int = Query(0, ge=0)) -> HTMLResponse:
    """Top-level communities. Children load per node, on first expand."""
    session = require_session(request)
    async with dspace_client(request, session) as client:
        communities = await client.top_communities(page=page, size=PAGE_SIZE)
    return render(
        request,
        "partials/communities.html",
        communities=communities.items,
        page=page,
        has_next=communities.has_next,
        depth=0,
    )


@router.get("/browse/node/{uuid}", response_class=HTMLResponse)
async def node(
    request: Request, uuid: str, page: int = Query(0, ge=0), depth: int = Query(0, ge=0)
) -> HTMLResponse:
    """One community's children: subcommunities (expandable) and collections."""
    session = require_session(request)
    async with dspace_client(request, session) as client:
        subcommunities = await client.subcommunities(uuid, page=page, size=PAGE_SIZE)
        collections = await client.collections_of(uuid, page=page, size=PAGE_SIZE)
    return render(
        request,
        "partials/node.html",
        uuid=uuid,
        subcommunities=subcommunities.items,
        collections=collections.items,
        page=page,
        has_next=subcommunities.has_next or collections.has_next,
        depth=min(depth + 1, 6),
    )


@router.get("/select/{uuid}")
async def select(request: Request, uuid: str, verified: bool = False) -> Response:
    """Choose a collection, gating on canSubmit unless the list was pre-filtered.

    `verified=1` is set only by links in *My collections*, whose entries DSpace
    already filtered. Forging it skips a UX check, not a real one -- an
    unauthorized collection still fails at create_workspaceitem in Phase 4.
    """
    session = require_session(request)
    async with dspace_client(request, session) as client:
        collection = await client.collection(uuid)
        allowed = True
        if not verified:
            allowed = await client.can_submit(collection.self_link or client.collection_uri(uuid))

    if not allowed:
        log.info("submit denied for %s to collection %s", session.eperson_name, uuid)
        response = render(request, "denied.html", collection=collection)
        response.status_code = 403
        return response

    session.collection_uuid = uuid
    session.data["collection_name"] = collection.name
    session.data["collection_handle"] = collection.handle
    request.app.state.sessions.save(session)
    return RedirectResponse("/submit", status_code=303)
