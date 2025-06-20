"""The review form, and the deposit (DESIGN.md §6 steps 6-8).

Everything on the form comes from DSpace: the sections, their fields, the input
types, which fields are mandatory, and the values already stored. ScholarDrop's
own contribution is the pre-fill -- the extractor's output, translated by the
instance's mapping profile, dropped into whichever section declares that metadata
field.

Depositing is three steps on purpose, and only the last one writes:

    /review/{id}          edit
    /review/{id}/confirm  read back the licence and a summary of what will be sent
    /review/{id}/deposit  PATCH the sections, then POST to workflowitems

Anything that goes wrong between the patch and the deposit leaves the draft
intact and says so; the form comes back with the user's own values and DSpace's
complaints against the fields that caused them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from scholardrop.dspace import DSpaceError, DSpaceValidationError
from scholardrop.routes.deps import dspace_client, render, require_session, session_instance
from scholardrop.submission.definition import (
    config_hrefs,
    load_form_configs,
    load_sections,
    load_vocabularies,
)
from scholardrop.submission.form import (
    FormField,
    FormValue,
    build_form,
    message_for,
    parse_fields,
)
from scholardrop.submission.patch import (
    build_operations,
    field_index,
    license_granted,
    parse_submission,
)

router = APIRouter()
log = logging.getLogger("scholardrop.review")


class _Configuration:
    """The live configuration one request needs, fetched once."""

    def __init__(
        self, item: Any, sections: list[dict[str, Any]], configs: dict, vocabularies: dict
    ):
        self.item = item
        self.sections = sections
        self.configs = configs
        self.vocabularies = vocabularies

    @property
    def allowed(self) -> dict[tuple[str, str], Any]:
        return field_index(self.sections, self.configs)


async def _load(client: Any, wsi_id: str, *, cache: Any, instance_key: str) -> _Configuration:
    item = await client.get_workspaceitem(wsi_id)
    sections = await load_sections(client, item, cache=cache, instance_key=instance_key)
    configs = await load_form_configs(client, sections, cache=cache, instance_key=instance_key)
    vocabularies = await load_vocabularies(client, configs, cache=cache, instance_key=instance_key)
    return _Configuration(item, sections, configs, vocabularies)


@router.get("/review/{wsi_id}", response_class=HTMLResponse)
async def review(request: Request, wsi_id: str) -> HTMLResponse:
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs

    async with dspace_client(request, session) as client:
        live = await _load(client, wsi_id, cache=cache, instance_key=instance.key)
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)

    job = request.app.state.extractions.get(session.id, wsi_id)
    # Only a finished extraction pre-fills. A running one leaves the form as
    # DSpace has it and says so -- the user can start typing meanwhile.
    mapped = job.mapped if job and job.state == "done" else None
    form = build_form(
        live.item,
        live.configs,
        sections=live.sections,
        mapped=mapped,
        vocabularies=live.vocabularies,
    )

    session.workspaceitem_id = live.item.id
    request.app.state.sessions.save(session)

    return render(
        request,
        "review.html",
        form=form,
        job=job,
        licence=licence,
        license_checked=form.license_granted,
        extraction=job.metadata if job else None,
        filename=(job.filename if job else None) or session.data.get("upload_filename"),
    )


@router.post("/review/{wsi_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, wsi_id: str) -> HTMLResponse:
    """Back from the confirm step, with every edit still in place."""
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs
    posted = list((await request.form()).multi_items())

    async with dspace_client(request, session) as client:
        live = await _load(client, wsi_id, cache=cache, instance_key=instance.key)
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)

    submitted = parse_submission(posted, live.allowed)
    form = build_form(
        live.item,
        live.configs,
        sections=live.sections,
        vocabularies=live.vocabularies,
        submitted=submitted,
    )
    return render(
        request,
        "review.html",
        form=form,
        licence=licence,
        filename=session.data.get("upload_filename"),
        license_checked=license_granted(posted),
    )


@router.post("/review/{wsi_id}/confirm", response_class=HTMLResponse)
async def confirm(request: Request, wsi_id: str) -> HTMLResponse:
    """The last look before anything is written: what will be sent, and the licence.

    Nothing has been patched at this point -- the values live in this page's
    hidden inputs, exactly as the user typed them.
    """
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs
    posted = list((await request.form()).multi_items())

    async with dspace_client(request, session) as client:
        live = await _load(client, wsi_id, cache=cache, instance_key=instance.key)
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)

    submitted = parse_submission(posted, live.allowed)
    form = build_form(
        live.item,
        live.configs,
        sections=live.sections,
        vocabularies=live.vocabularies,
        submitted=submitted,
    )
    return render(
        request,
        "confirm.html",
        form=form,
        licence=licence,
        license_checked=license_granted(posted),
        collection_name=session.data.get("collection_name"),
        filename=session.data.get("upload_filename"),
    )


@router.post("/review/{wsi_id}/deposit", response_class=HTMLResponse)
async def deposit(request: Request, wsi_id: str) -> Response:
    """Patch the sections, then hand the submission to the collection's workflow."""
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs
    posted = list((await request.form()).multi_items())
    granted = license_granted(posted)

    async with dspace_client(request, session) as client:
        live = await _load(client, wsi_id, cache=cache, instance_key=instance.key)
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)
        submitted = parse_submission(posted, live.allowed)

        def back(message: str, errors: dict[str, list[str]], status: int) -> HTMLResponse:
            form = build_form(
                live.item,
                live.configs,
                sections=live.sections,
                vocabularies=live.vocabularies,
                submitted=submitted,
                extra_errors=errors,
            )
            response = render(
                request,
                "review.html",
                form=form,
                licence=licence,
                filename=session.data.get("upload_filename"),
                license_checked=granted,
                deposit_error=message,
            )
            response.status_code = status
            return response

        if not granted:
            return back(
                "The deposit licence has to be granted before the submission can be sent.",
                {},
                400,
            )

        operations = build_operations(live.item, submitted, grant_license=True)
        try:
            patched = await client.patch_sections(wsi_id, operations)
        except DSpaceValidationError as exc:
            log.info("workspaceitem %s rejected the patch: %s", wsi_id, exc)
            return back(
                "The repository would not accept some of these values. Your draft is intact.",
                _messages(exc.field_messages()),
                400,
            )

        # DSpace reports incompleteness in the item rather than as a failure, so
        # this is the last chance to catch it before the deposit call.
        if patched.has_errors:
            log.info("workspaceitem %s is not ready to deposit", wsi_id)
            return back(
                "The submission is not complete yet. The repository is still asking for the "
                "fields marked below.",
                _messages({f: [i.message] for i in patched.errors for f in i.fields()}),
                400,
            )

        # Resolve the item *now*: the workspaceitem's own item link
        # (/submission/workspaceitems/{id}/item) 404s the moment the deposit
        # consumes the draft, and the archived case needs the uuid to read the
        # handle back.
        item_link = (patched.raw.get("_links") or {}).get("item", {}).get("href")
        submitted_item = None
        if item_link:
            try:
                submitted_item = await client.item(item_link)
            except DSpaceError as exc:
                log.info("could not resolve the item behind workspaceitem %s: %s", wsi_id, exc)

        try:
            result = await client.deposit(patched.self_link or live.item.self_link)
        except DSpaceValidationError as exc:
            log.info("deposit of workspaceitem %s was rejected: %s", wsi_id, exc)
            return back(
                "The repository refused the deposit. Nothing was lost — your draft is still "
                "here, with the problems marked below.",
                _messages(exc.field_messages()),
                400,
            )
        except DSpaceError as exc:
            log.error("deposit of workspaceitem %s failed: %s", wsi_id, exc)
            return back(
                "The deposit could not be completed. Your draft is intact — please try again.",
                {},
                502,
            )

        archived_item = None
        if result.archived and submitted_item and submitted_item.uuid:
            # Archived without review: the handle is minted at that moment, so it
            # is only readable from the item itself, by uuid.
            try:
                archived_item = await client.item(submitted_item.uuid)
            except DSpaceError as exc:
                log.info("deposited item could not be read back: %s", exc)

    log.info(
        "deposited workspaceitem %s for %s (%s)",
        wsi_id,
        session.eperson_name,
        "archived" if result.archived else "entered workflow",
    )
    request.app.state.extractions.forget(session.id, wsi_id)
    session.workspaceitem_id = None
    session.data.pop("upload_filename", None)
    # The repository has the file now, so the local copy has no further purpose
    # (DESIGN.md §8). Only this submission's file goes -- the session may still
    # be mid-flow on another one.
    stored = session.data.pop("upload_path", None)
    if stored:
        Path(stored).unlink(missing_ok=True)
    request.app.state.sessions.save(session)

    return render(
        request,
        "deposited.html",
        result=result,
        archived_item=archived_item,
        collection_name=session.data.get("collection_name"),
    )


@router.get("/review/{wsi_id}/row", response_class=HTMLResponse)
async def add_row(
    request: Request,
    wsi_id: str,
    section: str = Query(...),
    field: str = Query(...),
) -> HTMLResponse:
    """One more empty input for a repeatable field.

    The field's configuration is re-read rather than remembered, so a row can
    never be rendered with a shape the collection does not actually have.
    """
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs

    async with dspace_client(request, session) as client:
        item = await client.get_workspaceitem(wsi_id)
        sections = await load_sections(client, item, cache=cache, instance_key=instance.key)
        href = config_hrefs(sections).get(section)
        config = {}
        if href:
            key = (instance.key, href)
            config = cache.forms.get(key) or await client.submission_form(href)
            cache.forms[key] = config
        configs = {section: config}
        vocabularies = await load_vocabularies(
            client, configs, cache=cache, instance_key=instance.key
        )

    match = next((f for f in parse_fields(config) if f.metadata_field == field), None)
    if match is None or not match.repeatable:
        # Nothing to add: an unknown or non-repeatable field gets an empty swap
        # rather than an error page in the middle of a form.
        log.info("ignored add-row for %s/%s on workspaceitem %s", section, field, wsi_id)
        return HTMLResponse("")

    form_field = FormField(
        config=match,
        section_id=section,
        values=[FormValue()],
        options=vocabularies.get(match.vocabulary or "", []),
    )
    return render(request, "partials/field_row.html", field=form_field, value=FormValue())


async def _licence_text(client: Any, item: Any, *, cache: Any, instance_key: str) -> Any:
    """The collection's deposit licence, or None if it cannot be read.

    An unreadable licence must not block the page: the checkbox still states what
    granting means, and DSpace re-checks the grant at deposit anyway.
    """
    collection_uuid = item.sections.get("collection")
    if not isinstance(collection_uuid, str) or not collection_uuid:
        return None
    key = (instance_key, collection_uuid)
    if key in cache.licences:
        return cache.licences[key]
    try:
        licence = await client.collection_license(collection_uuid)
    except DSpaceError as exc:
        log.info("could not read the collection licence: %s", exc)
        licence = None
    cache.licences[key] = licence
    return licence


def _messages(field_messages: dict[str, list[str]]) -> dict[str, list[str]]:
    """DSpace's i18n keys, turned into sentences, keyed as the form expects."""
    return {field: [message_for(k) for k in keys] for field, keys in field_messages.items()}
