"""In-progress submissions: resume or discard (DESIGN.md §6, "abandoned drafts").

Creating the workspaceitem at upload time (§9, "State") buys resumability and
costs draft cleanup -- this is the cleanup. The list is DSpace's own
`findBySubmitter`, so it shows what the repository actually holds for this user,
including drafts started in the DSpace UI rather than here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from scholardrop.dspace import DSpaceError
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.routes.deps import DraftId, dspace_client, render, require_session

router = APIRouter()
log = logging.getLogger("scholardrop.drafts")

PAGE_SIZE = 10
TITLE_FIELD = "dc.title"


@dataclass
class Draft:
    """One row of the drafts list."""

    id: str
    title: str
    collection_name: str
    last_modified: str
    filenames: list[str]


def _first_value(sections: dict[str, Any], metadata_field: str) -> str:
    """The first value of a metadata field, wherever in the form it lives."""
    for data in sections.values():
        if not isinstance(data, dict):
            continue
        entries = data.get(metadata_field) or []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("value"):
                    return str(entry["value"])
    return ""


def _filenames(sections: dict[str, Any]) -> list[str]:
    upload = sections.get("upload")
    files = (upload or {}).get("files") if isinstance(upload, dict) else None
    names = []
    for entry in files or []:
        if isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
    return names


async def _to_draft(client: Any, item: WorkspaceItem, names: dict[str, str]) -> Draft:
    collection_uuid = item.sections.get("collection")
    collection_name = ""
    if isinstance(collection_uuid, str) and collection_uuid:
        if collection_uuid not in names:
            try:
                names[collection_uuid] = (await client.collection(collection_uuid)).name
            except DSpaceError as exc:
                # A draft whose collection we cannot read is still resumable.
                log.info("could not name collection %s: %s", collection_uuid, exc)
                names[collection_uuid] = ""
        collection_name = names[collection_uuid]
    return Draft(
        id=item.id,
        title=_first_value(item.sections, TITLE_FIELD),
        collection_name=collection_name,
        last_modified=(item.last_modified or "")[:10],
        filenames=_filenames(item.sections),
    )


@router.get("/drafts", response_class=HTMLResponse)
async def drafts(request: Request) -> HTMLResponse:
    require_session(request)
    return render(request, "drafts.html")


@router.get("/drafts/list", response_class=HTMLResponse)
async def drafts_list(request: Request, page: int = Query(0, ge=0)) -> HTMLResponse:
    session = require_session(request)
    names: dict[str, str] = {}
    async with dspace_client(request, session) as client:
        results = await client.find_by_submitter(session.eperson_uuid, page=page, size=PAGE_SIZE)
        items = [await _to_draft(client, item, names) for item in results.items]
    return render(
        request,
        "partials/drafts_list.html",
        drafts=items,
        total=results.total,
        page=page,
        has_next=results.has_next,
    )


@router.get("/drafts/{wsi_id}/discard", response_class=HTMLResponse)
async def confirm_discard(request: Request, wsi_id: DraftId) -> HTMLResponse:
    """Ask first: discarding deletes the draft and its file in DSpace."""
    require_session(request)
    return render(request, "partials/draft_confirm.html", wsi_id=wsi_id)


@router.post("/drafts/{wsi_id}/discard", response_class=HTMLResponse)
async def discard(request: Request, wsi_id: DraftId) -> HTMLResponse:
    session = require_session(request)
    async with dspace_client(request, session) as client:
        await client.delete_workspaceitem(wsi_id)
    log.info("discarded workspaceitem %s for %s", wsi_id, session.eperson_name)

    request.app.state.extractions.forget(session.id, wsi_id)
    if session.workspaceitem_id == wsi_id:
        session.workspaceitem_id = None
    # Discarding is an exit from the flow, so the local upload goes with it rather
    # than waiting for the TTL sweep -- and only this draft's, never another tab's.
    stored = session.forget_submission(wsi_id).get("path")
    if stored:
        Path(stored).unlink(missing_ok=True)
    request.app.state.sessions.save(session)

    return await drafts_list(request, page=0)
