"""Upload the PDF (DESIGN.md §6 step 5).

The order here is deliberate: the workspaceitem is created in DSpace **first**,
then the bitstream is attached, and only then does extraction start. That means
the collection's submission definition is known before the form renders, and an
abandoned upload leaves a resumable draft in DSpace rather than orphaned local
state. It also means the ACL is re-checked by DSpace itself at create time
(DESIGN.md §4) -- a 403 here is an access-denied event, not an error.

Extraction runs in the background while the browser polls, because it is the slow
part and it must never be able to block the submission.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from scholardrop.dspace import DSpaceError, DSpaceForbidden
from scholardrop.routes.deps import (
    DraftId,
    dspace_client,
    is_htmx,
    render,
    require_session,
    session_instance,
)
from scholardrop.submission.storage import UploadRejected, session_upload_dir, store_upload

router = APIRouter()
log = logging.getLogger("scholardrop.submit")


@router.get("/submit", response_class=HTMLResponse)
async def submit(request: Request) -> Response:
    session = require_session(request)
    if not session.collection_uuid:
        return RedirectResponse("/browse", status_code=303)
    return render(
        request,
        "submit.html",
        collection_uuid=session.collection_uuid,
        collection_name=session.data.get("collection_name"),
        collection_handle=session.data.get("collection_handle"),
        max_bytes=request.app.state.settings.upload_max_bytes,
        offer_restricted=offers_restricted(request),
    )


def offers_restricted(request: Request) -> bool:
    """Show the restricted-content checkbox only where it changes something.

    On an instance already configured for heuristic or local extraction the
    document never goes anywhere, so offering the choice would be theatre.
    """
    session = require_session(request)
    return session_instance(request, session).extraction == "cloud"


@router.post("/submit", response_class=HTMLResponse)
async def upload(
    request: Request,
    file: UploadFile | None = File(None),
    restricted: bool = Form(False),
) -> Response:
    session = require_session(request)
    if not session.collection_uuid:
        return RedirectResponse("/browse", status_code=303)

    settings = request.app.state.settings
    instance = session_instance(request, session)

    def rejected(message: str, status: int = 400) -> HTMLResponse:
        response = render(
            request,
            "partials/upload_form.html",
            error=message,
            restricted=restricted,
            offer_restricted=instance.extraction == "cloud",
        )
        response.status_code = status
        return response

    if file is None or not file.filename:
        return rejected("Choose the PDF you want to deposit.")

    try:
        stored = await store_upload(
            file,
            directory=session_upload_dir(settings.upload_dir, session),
            max_bytes=settings.upload_max_bytes,
        )
    except UploadRejected as exc:
        return rejected(exc.message, status=413 if "larger than" in exc.message else 400)
    finally:
        request.app.state.sessions.save(session)

    async with dspace_client(request, session) as client:
        try:
            item = await client.create_workspaceitem(session.collection_uuid)
        except DSpaceForbidden:
            # The gate in /select is UX; this is the wall (DESIGN.md §4).
            log.info(
                "create_workspaceitem denied for %s in collection %s",
                session.eperson_name,
                session.collection_uuid,
            )
            stored.path.unlink(missing_ok=True)
            response = render(
                request,
                "partials/denied_inline.html",
                collection_name=session.data.get("collection_name"),
            )
            response.status_code = 403
            return response

        try:
            # Reading it back is up to SCHOLARDROP_UPLOAD_MAX_BYTES off disk, which
            # is far too long to spend on the one event loop this process has.
            content = await asyncio.to_thread(stored.read_bytes)
            item = await client.upload_bitstream(item.id, stored.filename, content)
        except DSpaceError as exc:
            # Don't leave an empty draft behind for a failure the user did not
            # cause (DESIGN.md §7).
            log.error("upload failed for workspaceitem %s: %s", item.id, exc)
            try:
                await client.delete_workspaceitem(item.id)
            except DSpaceError as cleanup_exc:
                log.warning("could not remove workspaceitem %s: %s", item.id, cleanup_exc)
            stored.path.unlink(missing_ok=True)
            return rejected(
                "The repository would not accept that file. Nothing was created — "
                "please try again.",
                status=502,
            )

    session.workspaceitem_id = item.id
    # Keyed by workspaceitem: a second tab starting another submission must not
    # take this one's file or collection with it.
    session.remember_submission(item.id, filename=stored.filename, path=str(stored.path))
    request.app.state.sessions.save(session)

    profile = request.app.state.registry.profile_for(instance)
    request.app.state.extractions.start(
        session.id,
        item.id,
        request.app.state.extractor(
            stored.path,
            instance=instance,
            settings=settings,
            profile=profile,
            force_local=restricted,
        ),
        filename=stored.filename,
    )
    log.info(
        "workspaceitem %s created; extracting %s%s",
        item.id,
        stored.filename,
        " (restricted: local processing only)" if restricted else "",
    )

    if not is_htmx(request):
        # No JavaScript: skip the progress state and let /review report on the
        # extraction when it gets there.
        return RedirectResponse(f"/review/{item.id}", status_code=303)
    return render(
        request, "partials/upload_progress.html", wsi_id=item.id, filename=stored.filename
    )


@router.get("/submit/status/{wsi_id}", response_class=HTMLResponse)
async def status(request: Request, wsi_id: DraftId) -> Response:
    """Poll target: keep waiting, or send the browser to the review form."""
    session = require_session(request)
    job = request.app.state.extractions.get(session.id, wsi_id)

    if job is not None and not job.finished:
        return render(
            request, "partials/upload_progress.html", wsi_id=wsi_id, filename=job.filename
        )
    # No job (a restart, or a stale poll) is treated as finished -- the review
    # form renders either way, which is the promise extraction must keep.
    return Response(status_code=204, headers={"HX-Redirect": f"/review/{wsi_id}"})
