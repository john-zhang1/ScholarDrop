"""Upload the PDF (DESIGN.md §6 step 5).

The order here is deliberate: the workspaceitem is created in DSpace **first**,
then the bitstream is attached, and only then does extraction start. That means
the collection's submission definition is known before the form renders, and an
abandoned upload leaves a resumable draft in DSpace rather than orphaned local
state. It also means the ACL is re-checked by DSpace itself at create time
(DESIGN.md §4) -- a 403 here is an access-denied event, not an error.

Extraction runs in the background while the browser polls, because it is the slow
part and it must never be able to block the submission.

The exact-file duplicate check (DESIGN.md §12, layer 1) lands here too. It is a
*warning*: the draft and its file are already in DSpace by the time it is shown,
and both choices offered -- carry on, or discard -- are the user's to make.

Since Phase 8 the page accepts an **identifier** as well as (or instead of) a
file, per DESIGN.md §13.1. The draft is created the same way either way; the only
difference is whether the repository's own live-import provider made it from a
registry record. Everything about the identifier degrades: an unresolvable DOI
costs a notice, not the submission.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from scholardrop.dedup.check import exact_file_matches
from scholardrop.dspace import DSpaceError, DSpaceForbidden
from scholardrop.identifiers import Identifier, UnknownIdentifier, parse_identifier
from scholardrop.identifiers.sources import (
    NativeLookupFailed,
    NoNativeSource,
    available_sources,
    create_from_identifier,
)
from scholardrop.routes.deps import (
    DraftId,
    dspace_client,
    is_htmx,
    release_draft,
    render,
    require_session,
    session_instance,
)
from scholardrop.submission.defaults import apply_defaults, apply_seed
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
        offer_identifier=request.app.state.settings.identifiers_enabled,
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
    identifier: str = Form(""),
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
            identifier=identifier,
            offer_restricted=instance.extraction == "cloud",
            offer_identifier=settings.identifiers_enabled,
        )
        response.status_code = status
        return response

    typed = (identifier or "").strip()
    parsed: Identifier | None = None
    if typed and settings.identifiers_enabled:
        try:
            parsed = parse_identifier(typed)
        except UnknownIdentifier as exc:
            # Nothing has happened yet, and an identifier somebody typed that we
            # quietly ignored is the same sin as a dropped extracted value.
            return rejected(str(exc))

    has_file = file is not None and bool(file.filename)
    if not has_file and parsed is None:
        return rejected(
            "Choose a PDF to deposit, paste a DOI, arXiv ID or PubMed ID, or both."
            if settings.identifiers_enabled
            else "Choose the PDF you want to deposit."
        )

    stored = None
    if has_file:
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
            item, native_source = await _create_draft(request, client, session, parsed)
        except DSpaceForbidden:
            # The gate in /select is UX; this is the wall (DESIGN.md §4).
            log.info(
                "create_workspaceitem denied for %s in collection %s",
                session.eperson_name,
                session.collection_uuid,
            )
            if stored:
                stored.path.unlink(missing_ok=True)
            response = render(
                request,
                "partials/denied_inline.html",
                collection_name=session.data.get("collection_name"),
            )
            response.status_code = 403
            return response

        # Before the file, because a draft the repository refuses the bitstream
        # for is deleted below -- and after the identifier, because a registry
        # record outranks a default and this only fills what it left empty.
        await _seed_new_draft(request, client, instance, item)

        if stored is not None:
            try:
                # Reading it back is up to SCHOLARDROP_UPLOAD_MAX_BYTES off disk,
                # which is far too long to spend on the one event loop this
                # process has.
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

            # Layer 1, while the client is still open. It runs before extraction is
            # started so the answer is on screen at once (DESIGN.md §12), and it
            # never raises: `exact_file_matches` turns every failure into "no
            # warning".
            duplicates = await exact_file_matches(client, settings, instance, md5=stored.md5)
        else:
            # No file, no checksum, nothing to compare. The review page's title
            # check (layer 2) still runs.
            duplicates = []

    session.workspaceitem_id = item.id
    # Keyed by workspaceitem: a second tab starting another submission must not
    # take this one's file or collection with it.
    session.remember_submission(
        item.id,
        filename=stored.filename if stored else "",
        path=str(stored.path) if stored else "",
        identifier=parsed.display if parsed else "",
        identifier_url=parsed.url if parsed else "",
        identifier_source=native_source.id if native_source else "",
    )
    request.app.state.sessions.save(session)

    profile = request.app.state.registry.profile_for(instance)
    request.app.state.extractions.start(
        session.id,
        item.id,
        request.app.state.extractor(
            stored.path if stored else None,
            instance=instance,
            settings=settings,
            profile=profile,
            force_local=restricted,
            identifier=parsed,
            # The repository already resolved it and wrote the record into the
            # draft; asking the registry again would only produce values the
            # form is about to skip as "already filled".
            resolve_registry=native_source is None,
        ),
        # Strictly the file's name, empty when there is no file: the review page
        # decides what to say about a PDF from exactly this.
        filename=stored.filename if stored else "",
    )
    log.info(
        "workspaceitem %s created%s; %s%s",
        item.id,
        f" from {native_source.id} record {parsed.display}" if native_source and parsed else "",
        f"extracting {stored.filename}" if stored else "no file to extract",
        " (restricted: local processing only)" if restricted else "",
    )

    if duplicates:
        # The one place the flow deliberately stops. Not a block -- the draft is
        # made, the file is attached, extraction is running, and "Continue" is one
        # click -- but the progress panel auto-redirects to the review form, so a
        # warning rendered into it would be swept away before it was read.
        log.info(
            "workspaceitem %s duplicates %s by checksum",
            item.id,
            ", ".join(match.item_uuid for match in duplicates),
        )
        template = "partials/duplicate_file.html" if is_htmx(request) else "duplicate_file.html"
        return render(
            request,
            template,
            wsi_id=item.id,
            filename=stored.filename if stored else "",
            matches=duplicates,
        )

    if not is_htmx(request):
        # No JavaScript: skip the progress state and let /review report on the
        # extraction when it gets there.
        return RedirectResponse(f"/review/{item.id}", status_code=303)
    return render(
        request,
        "partials/upload_progress.html",
        wsi_id=item.id,
        filename=stored.filename if stored else "",
        identifier=parsed.display if parsed else "",
    )


async def _create_draft(
    request: Request,
    client: Any,
    session: Any,
    parsed: Identifier | None,
) -> tuple[Any, Any]:
    """Start the draft, letting the repository resolve the identifier if it can.

    Returns the workspaceitem and the provider that made it, or `None` for the
    provider when the draft was created empty. Every way the native path can fail
    -- no provider configured, no record, an upstream that will not answer --
    ends in the same plain draft, because the identifier still has a second
    chance in `resolvers.py` and the submission must not depend on either.

    `DSpaceForbidden` is left to propagate: that is the ACL wall, and it belongs
    to the caller's access-denied path.
    """
    settings = request.app.state.settings
    instance = session_instance(request, session)
    collection_uuid = session.collection_uuid

    if parsed is not None and settings.identifiers_native_source_enabled:
        sources = await available_sources(
            client, cache=request.app.state.form_configs, instance_key=instance.key
        )
        try:
            return await create_from_identifier(client, collection_uuid, parsed, sources)
        except NoNativeSource as exc:
            log.info("%s: %s", instance.key, exc)
        except NativeLookupFailed as exc:
            log.info(
                "the repository could not resolve %s (%s); asking the registry directly",
                parsed.display,
                exc,
            )

    return await client.create_workspaceitem(collection_uuid), None


async def _seed_new_draft(request: Request, client: Any, instance: Any, item: Any) -> None:
    """Give a brand-new draft the values nobody should have to type (DESIGN.md §13.4)."""
    if not request.app.state.settings.defaults_enabled:
        return
    await apply_defaults(
        client,
        item,
        profile=request.app.state.registry.profile_for(instance),
        cache=request.app.state.form_configs,
        instance_key=instance.key,
    )


@router.post("/submit/like", response_class=HTMLResponse)
async def submit_like(request: Request) -> Response:
    """Start a draft like the one just deposited (DESIGN.md §13.4).

    The seed is written by the deposit that produced it and lives in the session,
    so this creates a real draft in the same collection with the shared context
    already in place -- and nothing that belongs to the deposited work. It is the
    ordinary submission flow from there: the review form, the confirm step, and an
    explicit deposit.
    """
    session = require_session(request)
    instance = session_instance(request, session)
    seed = session.data.get("submit_like") or {}
    collection_uuid = str(seed.get("collection_uuid") or "")
    if not request.app.state.settings.defaults_enabled or not collection_uuid:
        # Nothing to seed from -- a restarted process, or a bookmarked POST.
        return RedirectResponse("/browse", status_code=303)

    async with dspace_client(request, session) as client:
        try:
            item = await client.create_workspaceitem(collection_uuid)
        except DSpaceForbidden:
            log.info(
                "create_workspaceitem denied for %s in collection %s",
                session.eperson_name,
                collection_uuid,
            )
            response = render(
                request,
                "partials/denied_inline.html",
                collection_name=seed.get("collection_name"),
            )
            response.status_code = 403
            return response
        await apply_seed(
            client,
            item,
            seed.get("values") or {},
            cache=request.app.state.form_configs,
            instance_key=instance.key,
        )

    session.collection_uuid = collection_uuid
    session.data["collection_name"] = seed.get("collection_name")
    session.workspaceitem_id = item.id
    # No file and no extraction: this draft's values came from the last deposit,
    # and the review form renders perfectly well with no job behind it.
    session.remember_submission(item.id, filename="", path="")
    request.app.state.sessions.save(session)
    log.info("draft %s started from the deposit before it", item.id)

    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": f"/review/{item.id}"})
    return RedirectResponse(f"/review/{item.id}", status_code=303)


@router.post("/submit/{wsi_id}/continue", response_class=HTMLResponse)
async def proceed(request: Request, wsi_id: DraftId, warning: str = Query("duplicate")) -> Response:
    """The user read the warning and decided to submit anyway.

    A POST rather than a link, and logged, because "somebody was told this file is
    already here and deposited it regardless" is exactly the thing a repository
    manager asks about afterwards. Which warning it was is logged with it: the
    duplicate one (DESIGN.md §12) and the scanned-PDF one (§13.4) share this exit,
    and they are not the same event.
    """
    session = require_session(request)
    log.info(
        "%s continued past a %s warning on workspaceitem %s",
        session.eperson_name,
        "duplicate" if warning not in {"duplicate", "scan"} else warning,
        wsi_id,
    )
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": f"/review/{wsi_id}"})
    return RedirectResponse(f"/review/{wsi_id}", status_code=303)


@router.post("/submit/{wsi_id}/cancel", response_class=HTMLResponse)
async def cancel(request: Request, wsi_id: DraftId) -> Response:
    """Discard the draft the duplicate warning was about, and offer another file.

    The draft exists in DSpace by the time the warning is shown, so backing out
    has to delete it -- otherwise every dismissed warning leaves a half-made
    submission behind, which is exactly the clutter `/drafts` and
    `scripts/prune_drafts.py` already exist to clean up.

    Whose draft it is, is DSpace's question and not ours: the DELETE goes out as
    the signed-in user, so somebody else's id answers 403 and lands in the failure
    path below. `release_draft` only ever touches *this* session's own state, so
    there is nothing for a guessed id to reach here either.
    """
    session = require_session(request)
    failed = False
    async with dspace_client(request, session) as client:
        try:
            await client.delete_workspaceitem(wsi_id)
        except DSpaceError as exc:
            # The draft may simply be un-deletable (a DSpace bug, a policy, or not
            # this user's). Say so and point at /drafts rather than pretending it
            # is gone.
            log.warning(
                "could not discard workspaceitem %s after a duplicate warning: %s", wsi_id, exc
            )
            failed = True
        else:
            log.info("discarded workspaceitem %s after a duplicate warning", wsi_id)

    release_draft(request, session, wsi_id)

    if failed:
        response = render(
            request,
            "partials/upload_form.html",
            error="The upload was abandoned, but the repository would not remove "
            "the draft it had already created. It is listed under Drafts.",
            offer_restricted=offers_restricted(request),
        )
        response.status_code = 502
        return response
    if is_htmx(request):
        return render(
            request, "partials/upload_form.html", offer_restricted=offers_restricted(request)
        )
    return RedirectResponse("/submit", status_code=303)


@router.get("/submit/status/{wsi_id}", response_class=HTMLResponse)
async def status(request: Request, wsi_id: DraftId) -> Response:
    """Poll target: keep waiting, or send the browser to the review form."""
    session = require_session(request)
    job = request.app.state.extractions.get(session.id, wsi_id)

    if job is not None and not job.finished:
        return render(
            request,
            "partials/upload_progress.html",
            wsi_id=wsi_id,
            filename=job.filename,
            # A submission started from an identifier has no filename to show, and
            # the session is where the identifier lives.
            identifier=session.submission(wsi_id).get("identifier"),
        )

    # A scan is worth one click of the user's attention (DESIGN.md §13.4), and
    # this is the last moment there is one: the next thing this panel does is
    # redirect. The draft, the file and the extraction are all done -- nothing
    # here refuses anything.
    if job is not None and job.scanned and request.app.state.settings.scanned_warning_enabled:
        log.info("workspaceitem %s has no text layer; warning before the form", wsi_id)
        return render(
            request,
            "partials/scanned_pdf.html",
            wsi_id=wsi_id,
            filename=job.filename,
        )

    # No job (a restart, or a stale poll) is treated as finished -- the review
    # form renders either way, which is the promise extraction must keep.
    return Response(status_code=204, headers={"HX-Redirect": f"/review/{wsi_id}"})
