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
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from scholardrop.dedup.check import title_matches
from scholardrop.dedup.matches import matches_from_section, section_is_configured
from scholardrop.dspace import DSpaceError, DSpaceValidationError
from scholardrop.routes.deps import (
    DraftId,
    dspace_client,
    release_draft,
    render,
    require_session,
    session_instance,
)
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
async def review(request: Request, wsi_id: DraftId) -> HTMLResponse:
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs

    job = request.app.state.extractions.get(session.id, wsi_id)
    # Only a finished extraction pre-fills. A running one leaves the form as
    # DSpace has it and says so -- the user can start typing meanwhile.
    mapped = job.mapped if job and job.state == "done" else None

    async with dspace_client(request, session) as client:
        live = await _load(client, wsi_id, cache=cache, instance_key=instance.key)
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)
        form = build_form(
            live.item,
            live.configs,
            sections=live.sections,
            mapped=mapped,
            vocabularies=live.vocabularies,
        )
        # Inside the block: the duplicate check may need a Discovery query, and
        # building a second client for it would mean a second CSRF/JWT round trip
        # for one search.
        duplicates = await _duplicates(request, client, instance, live.item, form)

    session.workspaceitem_id = live.item.id
    request.app.state.sessions.save(session)

    draft = session.submission(wsi_id)
    return render(
        request,
        "review.html",
        form=form,
        job=job,
        licence=licence,
        license_checked=form.license_granted,
        extraction=job.metadata if job else None,
        filename=(job.filename if job else None) or draft.get("filename"),
        duplicates=duplicates,
        # The identifier this draft was started from, if it was (DESIGN.md §13.1).
        # Read from the session, so a draft resumed in another session simply does
        # not carry the banner -- the values are stored in the item either way.
        identifier=draft.get("identifier"),
        identifier_url=draft.get("identifier_url"),
        identifier_source=draft.get("identifier_source"),
        registry_notice=job.registry_notice if job else None,
    )


@router.post("/review/{wsi_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, wsi_id: DraftId) -> HTMLResponse:
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
        filename=session.submission(wsi_id).get("filename"),
        license_checked=license_granted(posted),
    )


@router.post("/review/{wsi_id}/confirm", response_class=HTMLResponse)
async def confirm(request: Request, wsi_id: DraftId) -> HTMLResponse:
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
        collection = await _collection_name(
            client, live.item, cache=cache, instance_key=instance.key
        )

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
        collection_name=collection,
        filename=session.submission(wsi_id).get("filename"),
    )


@router.post("/review/{wsi_id}/deposit", response_class=HTMLResponse)
async def deposit(request: Request, wsi_id: DraftId) -> Response:
    """Patch the sections, then hand the submission to the collection's workflow."""
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs
    posted = list((await request.form()).multi_items())
    granted = license_granted(posted)

    async with dspace_client(request, session) as client:
        live = await _load(client, wsi_id, cache=cache, instance_key=instance.key)
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)
        collection = await _collection_name(
            client, live.item, cache=cache, instance_key=instance.key
        )
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
                filename=session.submission(wsi_id).get("filename"),
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
    # The repository has the file now, so the local copy has no further purpose
    # (DESIGN.md §8).
    release_draft(request, session, wsi_id)

    return render(
        request,
        "deposited.html",
        result=result,
        archived_item=archived_item,
        collection_name=collection,
    )


@router.get("/review/{wsi_id}/row", response_class=HTMLResponse)
async def add_row(
    request: Request,
    wsi_id: DraftId,
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
    # row_index=None: this row is appended, so it is never the first one and must
    # not repeat the id the field's <label for> points at.
    return render(
        request,
        "partials/field_row.html",
        field=form_field,
        value=FormValue(),
        row_index=None,
    )


def title_on(form: Any, profile: Any) -> str:
    """The title this draft currently claims, prefill included.

    Read off the built form rather than out of the workspaceitem, because on the
    first visit the extracted title has not been saved yet -- and a duplicate
    panel that only worked on a resumed draft would be missing from every
    submission it was designed for.

    Which metadata field carries a title is the instance's mapping profile's
    answer, not a constant here (`dc.title` is only the usual one).
    """
    metadata_field = profile.metadata_field("title") or "dc.title"
    for section in form.editable_sections:
        for field_ in section.fields:
            if field_.config.metadata_field != metadata_field:
                continue
            for value in field_.values:
                if value.value.strip():
                    return value.value.strip()
    return ""


async def _duplicates(
    request: Request, client: Any, instance: Any, item: Any, form: Any
) -> list[Any]:
    """Anything already in the repository that this draft might repeat.

    Two sources, and the repository's own is always preferred: DSpace's Basic
    Duplicate Detection is a Solr query against normalised titles with the
    repository's own configured tolerance, which is a better answer than a phrase
    search filtered here. ScholarDrop only asks for itself when the step is not
    configured at all -- an *empty* section means the detector ran and found
    nothing, and overriding that with a cruder check would be replacing a good
    answer with a worse one.

    Never raises, and the blanket `except` is the point rather than an oversight:
    this panel is decoration on a page that must render, and the review form is
    where a user's unsaved edits live. Losing the form to a fault in an advisory
    extra would cost them work; losing the extra costs a warning they can still get
    at deposit time.
    """
    settings = request.app.state.settings
    if not settings.dedup_enabled:
        return []
    try:
        if section_is_configured(item.sections):
            return matches_from_section(item.sections, url_for=instance.item_url)[
                : settings.dedup_max_matches
            ]

        title = title_on(form, request.app.state.registry.profile_for(instance))
        if not title:
            return []
        # The draft's own item is not archived yet, so it should not come back from
        # Discovery -- but a resumed draft on a versioned item could, and naming a
        # submission as its own duplicate would be pure noise.
        own_uuid = str((item.raw.get("_links") or {}).get("item", {}).get("href", "")).rsplit(
            "/", 1
        )[-1]
        return await title_matches(client, settings, instance, title=title, exclude_uuid=own_uuid)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        log.warning(
            "duplicate check failed for workspaceitem %s (%s: %s); rendering without it",
            item.id,
            type(exc).__name__,
            exc,
        )
        return []


async def _collection_name(client: Any, item: Any, *, cache: Any, instance_key: str) -> str:
    """The name of the collection *this draft* is going to.

    Read from the draft rather than remembered from the session: a session has one
    "collection I just picked", but a user can be part-way through several drafts,
    and a resumed one may belong to a collection they chose days ago. Naming the
    wrong collection on the page that asks "shall I deposit this?" is exactly the
    kind of quiet wrongness the confirm step exists to prevent.
    """
    collection_uuid = item.sections.get("collection")
    if not isinstance(collection_uuid, str) or not collection_uuid:
        return ""
    key = (instance_key, collection_uuid)
    if key not in cache.collection_names:
        try:
            cache.collection_names[key] = (await client.collection(collection_uuid)).name
        except DSpaceError as exc:
            # Not worth failing the page over; the templates all read "or ...".
            log.info("could not name collection %s: %s", collection_uuid, exc)
            cache.collection_names[key] = ""
    return cache.collection_names[key]


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
