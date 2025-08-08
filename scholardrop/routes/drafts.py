"""In-progress submissions: resume or discard (DESIGN.md §6, "abandoned drafts").

Creating the workspaceitem at upload time (§9, "State") buys resumability and
costs draft cleanup -- this is the cleanup. The list is DSpace's own
`findBySubmitter`, so it shows what the repository actually holds for this user,
including drafts started in the DSpace UI rather than here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from scholardrop.dspace import DSpaceError
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.routes.deps import (
    DraftId,
    dspace_client,
    render,
    require_session,
    session_instance,
)
from scholardrop.submission.definition import load_form_configs, load_sections
from scholardrop.submission.readiness import Readiness, assess, labels_from

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
    readiness: Readiness = field(default_factory=Readiness)


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


async def _labels(
    client: Any, item: WorkspaceItem, *, cache: Any, instance_key: str, memo: dict[str, Any]
) -> tuple[dict[str, str], bool]:
    """Field labels for this draft's collection, or nothing and an honest flag.

    Keyed by the submission definition rather than the collection, because two
    collections sharing a definition share their labels. `memo` spans the page;
    the `ConfigCache` underneath spans the hour, so the second page of drafts
    usually fetches nothing.
    """
    definition = (item.raw.get("_links") or {}).get("submissionDefinition", {}).get("href", "")
    key = str(definition or item.sections.get("collection") or item.id)
    if key in memo:
        labels = memo[key]
        return (labels, True) if labels is not None else ({}, False)

    try:
        sections = await load_sections(client, item, cache=cache, instance_key=instance_key)
        configs = await load_form_configs(client, sections, cache=cache, instance_key=instance_key)
        labels = labels_from(configs)
    except DSpaceError as exc:
        # A draft whose form config we cannot read still gets to say what is
        # wrong with it, in DSpace's own field names. Never take the list down.
        log.info("could not read the form config for draft %s: %s", item.id, exc)
        memo[key] = None
        return {}, False

    memo[key] = labels
    return labels, True


async def _to_draft(
    client: Any,
    item: WorkspaceItem,
    names: dict[str, str],
    *,
    cache: Any,
    instance_key: str,
    labels_memo: dict[str, Any],
) -> Draft:
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

    labels, labelled = await _labels(
        client, item, cache=cache, instance_key=instance_key, memo=labels_memo
    )
    return Draft(
        id=item.id,
        title=_first_value(item.sections, TITLE_FIELD),
        collection_name=collection_name,
        last_modified=(item.last_modified or "")[:10],
        filenames=_filenames(item.sections),
        readiness=assess(item, labels, labelled=labelled),
    )


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
            await _to_draft(
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
        draft = await _to_draft(
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
