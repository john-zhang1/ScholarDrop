"""In-progress submissions: resume or discard (DESIGN.md §6, "abandoned drafts").

Creating the workspaceitem at upload time (§9, "State") buys resumability and
costs draft cleanup -- this is the cleanup. The list is DSpace's own
`findBySubmitter`, so it shows what the repository actually holds for this user,
including drafts started in the DSpace UI rather than here.

The row itself is built in `submission/drafts.py`, because the status page
(§13.3) renders the same thing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from scholardrop.routes.deps import (
    DraftId,
    dspace_client,
    release_draft,
    render,
    require_session,
    session_instance,
)
from scholardrop.submission.drafts import Draft, to_draft
from scholardrop.timestamps import format_timestamp

__all__ = ["PAGE_SIZE", "Draft", "format_timestamp", "router"]

router = APIRouter()
log = logging.getLogger("scholardrop.drafts")

PAGE_SIZE = 10


@router.get("/drafts", response_class=HTMLResponse)
async def drafts(request: Request) -> HTMLResponse:
    require_session(request)
    return render(request, "drafts.html")


@router.get("/drafts/list", response_class=HTMLResponse)
async def drafts_list(request: Request, page: int = Query(0, ge=0)) -> HTMLResponse:
    session = require_session(request)
    instance = session_instance(request, session)
    names: dict[str, str] = {}
    labels_memo: dict[str, Any] = {}
    async with dspace_client(request, session) as client:
        results = await client.find_by_submitter(session.eperson_uuid, page=page, size=PAGE_SIZE)
        items = [
            await to_draft(
                client,
                item,
                names,
                cache=request.app.state.form_configs,
                instance_key=instance.key,
                labels_memo=labels_memo,
            )
            for item in results.items
        ]
    return render(
        request,
        "partials/drafts_list.html",
        drafts=items,
        total=results.total,
        page=page,
        has_next=results.has_next,
        ready_count=sum(1 for draft in items if draft.readiness.ready),
    )


@router.get("/drafts/{wsi_id}/discard", response_class=HTMLResponse)
async def confirm_discard(request: Request, wsi_id: DraftId) -> HTMLResponse:
    """Ask first: discarding deletes the draft and its file in DSpace."""
    require_session(request)
    return render(request, "partials/draft_confirm.html", wsi_id=wsi_id)


@router.get("/drafts/{wsi_id}/actions", response_class=HTMLResponse)
async def draft_actions(request: Request, wsi_id: DraftId) -> HTMLResponse:
    """One row's buttons again, after the user decides not to discard it.

    Backing out of a confirmation used to re-render the whole list, which reads
    every draft and every collection's form configuration to answer a question
    the user just declined to ask. This is that row and nothing else.
    """
    session = require_session(request)
    async with dspace_client(request, session) as client:
        item = await client.get_workspaceitem(wsi_id)
        draft = await to_draft(
            client,
            item,
            {},
            cache=request.app.state.form_configs,
            instance_key=session_instance(request, session).key,
            labels_memo={},
        )
    return render(request, "partials/draft_actions.html", draft=draft)


@router.post("/drafts/{wsi_id}/discard", response_class=HTMLResponse)
async def discard(request: Request, wsi_id: DraftId) -> HTMLResponse:
    session = require_session(request)
    async with dspace_client(request, session) as client:
        await client.delete_workspaceitem(wsi_id)
    log.info("discarded workspaceitem %s for %s", wsi_id, session.eperson_name)
    # Discarding is an exit from the flow, so the local upload goes with it rather
    # than waiting for the TTL sweep.
    release_draft(request, session, wsi_id)
    return await drafts_list(request, page=0)
