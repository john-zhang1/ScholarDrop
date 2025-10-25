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
import re
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from scholardrop import orcid as orcid_lookup
from scholardrop.dedup.check import title_matches
from scholardrop.dedup.matches import matches_from_section, section_is_configured
from scholardrop.dspace import DSpaceError, DSpaceValidationError
from scholardrop.routes.deps import (
    DraftId,
    dspace_client,
    record_deposit,
    release_draft,
    render,
    require_session,
    session_instance,
)
from scholardrop.submission.access import (
    build_operations as access_operations,
)
from scholardrop.submission.access import (
    describe_choices,
    input_name,
    parse_choices,
    parse_options,
    rows_for,
    validate_choices,
)
from scholardrop.submission.defaults import carry_over
from scholardrop.submission.definition import (
    config_hrefs,
    load_form_configs,
    load_sections,
    load_upload_config,
    load_vocabularies,
    upload_section,
)
from scholardrop.submission.drafts import first_value
from scholardrop.submission.form import (
    FormField,
    FormValue,
    UploadedFile,
    build_form,
    message_for,
    parse_fields,
)
from scholardrop.submission.patch import (
    build_operations,
    field_index,
    license_granted,
    parse_languages,
    parse_orcids,
    parse_submission,
)

router = APIRouter()
log = logging.getLogger("scholardrop.review")

TITLE_FIELD = "dc.title"
# What DSpace's own configuration allows a vocabulary to be called; this one is
# interpolated into a request path, so it is checked rather than trusted.
_VOCABULARY_NAME = re.compile(r"[A-Za-z0-9][\w.:-]{0,63}")


def _submitted_title(submitted: dict[tuple[str, str], list[str]]) -> str:
    """The title as the user approved it, whichever section carries the field.

    A fallback for the log row (DESIGN.md §13.3), because the patched item is
    the *repository's* echo -- and a title we never read back is still a title
    that was deposited. The values here are the ones actually sent.
    """
    for (_section, field), values in submitted.items():
        if field == TITLE_FIELD and values:
            return values[0]
    return ""


class _Configuration:
    """The live configuration one request needs, fetched once."""

    def __init__(
        self,
        item: Any,
        sections: list[dict[str, Any]],
        configs: dict,
        vocabularies: dict,
        upload_section_id: str = "",
        access_options: list[Any] | None = None,
    ):
        self.item = item
        self.sections = sections
        self.configs = configs
        self.vocabularies = vocabularies
        # DESIGN.md §13.5. The section id is read from the definition because the
        # patch path names it, and a definition may call its upload step anything.
        self.upload_section_id = upload_section_id
        self.access_options = access_options or []

    @property
    def allowed(self) -> dict[tuple[str, str], Any]:
        return field_index(self.sections, self.configs)


async def _load(
    client: Any, wsi_id: str, *, cache: Any, instance_key: str, access: bool = False
) -> _Configuration:
    item = await client.get_workspaceitem(wsi_id)
    sections = await load_sections(client, item, cache=cache, instance_key=instance_key)
    configs = await load_form_configs(client, sections, cache=cache, instance_key=instance_key)
    vocabularies = await load_vocabularies(client, configs, cache=cache, instance_key=instance_key)
    section_id, options = "", []
    if access:
        # One extra read per instance, cached with the rest of the repository's
        # configuration; skipped entirely when the slice is switched off.
        section_id, _href = upload_section(sections)
        options = parse_options(
            await load_upload_config(client, sections, cache=cache, instance_key=instance_key)
        )
    return _Configuration(item, sections, configs, vocabularies, section_id, options)


def _typeahead(request: Request) -> bool:
    """Whether a vocabulary-bound field searches its vocabulary (DESIGN.md §13.4)."""
    return bool(request.app.state.settings.vocabulary_typeahead_enabled)


def _access(request: Request) -> bool:
    """Whether a file may be given an access condition here (DESIGN.md §13.5)."""
    return bool(request.app.state.settings.access_conditions_enabled)


def _uploaded_files(item: Any, upload_section_id: str) -> list[UploadedFile]:
    """The draft's files, in the order the access-condition patch path counts them.

    That path names a file by its *position*, so the list is read from the item
    the deposit is about to patch rather than from the page that was rendered —
    which is what keeps a stale form from putting a policy on a file that is not
    there, or, after a re-upload, on a different one.
    """
    section = item.sections.get(upload_section_id) if upload_section_id else None
    files = (section or {}).get("files") if isinstance(section, dict) else None
    if not isinstance(files, list):
        return []
    return [UploadedFile.from_payload(f) for f in files if isinstance(f, dict)]


def _translatable(request: Request, instance: Any) -> frozenset[str]:
    """Which fields may carry a parallel value in another language (§13.4).

    The instance's mapping profile decides -- `dc.title` is only the usual name
    for a title -- and the slice's flag decides whether the offer is made at all.
    """
    if not request.app.state.settings.bilingual_enabled:
        return frozenset()
    return request.app.state.registry.profile_for(instance).translatable_fields()


def _orcid_policy(request: Request, instance: Any, live: _Configuration) -> Any:
    """This instance's ORCID storage mode, read against this collection's form."""
    return orcid_lookup.policy_for(
        instance,
        request.app.state.registry.profile_for(instance),
        live.allowed,
        enabled=request.app.state.settings.orcid_enabled,
    )


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
        live = await _load(
            client, wsi_id, cache=cache, instance_key=instance.key, access=_access(request)
        )
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)
        policy = _orcid_policy(request, instance, live)
        form = build_form(
            live.item,
            live.configs,
            sections=live.sections,
            mapped=mapped,
            vocabularies=live.vocabularies,
            orcid_policy=policy,
            vocabulary_typeahead=_typeahead(request),
            translatable_fields=_translatable(request, instance),
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
        # The PDF had no text layer (DESIGN.md §13.4). Said on the form as well as
        # on the progress screen: without JavaScript the upload lands here directly.
        scanned=bool(job and job.scanned and request.app.state.settings.scanned_warning_enabled),
        orcid_policy=policy,
        # One picker per uploaded file, or nothing at all where the collection
        # configures no access conditions (DESIGN.md §13.5).
        access_rows=rows_for(form.upload_files, live.access_options),
    )


@router.post("/review/{wsi_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, wsi_id: DraftId) -> HTMLResponse:
    """Back from the confirm step, with every edit still in place."""
    session = require_session(request)
    instance = session_instance(request, session)
    cache = request.app.state.form_configs
    posted = list((await request.form()).multi_items())

    async with dspace_client(request, session) as client:
        live = await _load(
            client, wsi_id, cache=cache, instance_key=instance.key, access=_access(request)
        )
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)

    policy = _orcid_policy(request, instance, live)
    submitted = parse_submission(posted, live.allowed)
    form = build_form(
        live.item,
        live.configs,
        sections=live.sections,
        vocabularies=live.vocabularies,
        submitted=submitted,
        submitted_orcids=parse_orcids(posted, live.allowed),
        submitted_languages=parse_languages(posted, live.allowed),
        orcid_policy=policy,
        vocabulary_typeahead=_typeahead(request),
        translatable_fields=_translatable(request, instance),
    )
    return render(
        request,
        "review.html",
        form=form,
        licence=licence,
        filename=session.submission(wsi_id).get("filename"),
        license_checked=license_granted(posted),
        orcid_policy=policy,
        # The access choices come back with everything else, or "back to editing"
        # would quietly reset them to the collection's default (DESIGN.md §13.5).
        access_rows=rows_for(form.upload_files, live.access_options, choices=parse_choices(posted)),
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
        live = await _load(
            client, wsi_id, cache=cache, instance_key=instance.key, access=_access(request)
        )
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)
        collection = await _collection_name(
            client, live.item, cache=cache, instance_key=instance.key
        )

    policy = _orcid_policy(request, instance, live)
    submitted = parse_submission(posted, live.allowed)
    form = build_form(
        live.item,
        live.configs,
        sections=live.sections,
        vocabularies=live.vocabularies,
        submitted=submitted,
        submitted_orcids=parse_orcids(posted, live.allowed),
        submitted_languages=parse_languages(posted, live.allowed),
        orcid_policy=policy,
        vocabulary_typeahead=_typeahead(request),
        translatable_fields=_translatable(request, instance),
    )
    return render(
        request,
        "confirm.html",
        orcid_policy=policy,
        form=form,
        licence=licence,
        license_checked=license_granted(posted),
        collection_name=collection,
        filename=session.submission(wsi_id).get("filename"),
        # Carried as hidden inputs and stated in words: this page is the last
        # look at what will be sent, and access is part of that (DESIGN.md §13.5).
        access_rows=rows_for(form.upload_files, live.access_options, choices=parse_choices(posted)),
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
        live = await _load(
            client, wsi_id, cache=cache, instance_key=instance.key, access=_access(request)
        )
        licence = await _licence_text(client, live.item, cache=cache, instance_key=instance.key)
        collection = await _collection_name(
            client, live.item, cache=cache, instance_key=instance.key
        )
        policy = _orcid_policy(request, instance, live)
        submitted = parse_submission(posted, live.allowed)
        orcids = parse_orcids(posted, live.allowed)
        languages = parse_languages(posted, live.allowed)
        # With the slice off, nothing rendered a picker, so any access input in
        # the POST is stale or crafted -- ignored wholesale, like the flag says.
        access = parse_choices(posted) if _access(request) else {}
        # The files the *item* holds right now, which is what bounds every use
        # of a posted file index: validation, the patch, and the confirmation.
        deposited_files = _uploaded_files(live.item, live.upload_section_id)
        deposited_names = {i: f.name for i, f in enumerate(deposited_files)}

        def back(
            message: str,
            errors: dict[str, list[str]],
            status: int,
            access_problems: dict[int, str] | None = None,
        ) -> HTMLResponse:
            form = build_form(
                live.item,
                live.configs,
                sections=live.sections,
                vocabularies=live.vocabularies,
                submitted=submitted,
                submitted_orcids=orcids,
                submitted_languages=languages,
                extra_errors=errors,
                orcid_policy=policy,
                vocabulary_typeahead=_typeahead(request),
                translatable_fields=_translatable(request, instance),
            )
            response = render(
                request,
                "review.html",
                form=form,
                licence=licence,
                filename=session.submission(wsi_id).get("filename"),
                license_checked=granted,
                deposit_error=message,
                orcid_policy=policy,
                access_rows=rows_for(
                    form.upload_files,
                    live.access_options,
                    choices=access,
                    problems=access_problems,
                ),
            )
            response.status_code = status
            return response

        if not granted:
            return back(
                "The deposit licence has to be granted before the submission can be sent.",
                {},
                400,
            )

        # Checked before anything is written, and with the repository's own rules
        # (DESIGN.md §13.5): an embargo with no date, one beyond the configured
        # limit, or one naming a file the draft no longer holds is a sentence
        # beside the file rather than a 422 at the end -- or, worse, a silently
        # dropped choice the confirmation would still have claimed.
        access_problems = validate_choices(access, live.access_options, files=deposited_files)
        if access_problems:
            log.info("workspaceitem %s has an unusable access condition", wsi_id)
            # A problem naming a file that is no longer there has no row to sit
            # beside, so the banner has to carry that sentence itself.
            stale = any(not 0 <= i < len(deposited_files) for i in access_problems)
            return back(
                "The files on this draft have changed, so the access choices need another look."
                if stale
                else "The access condition on one of these files cannot be applied as it stands.",
                {},
                400,
                access_problems,
            )

        operations = build_operations(
            live.item,
            submitted,
            grant_license=True,
            orcids=orcids,
            languages=languages,
            policy=policy,
        )
        # After the metadata, because a file's policies are only worth setting on
        # a draft the repository is going to accept -- and one patch request
        # carries both.
        operations.extend(
            access_operations(
                live.upload_section_id,
                access,
                live.access_options,
                files=deposited_files,
            )
        )
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
    # Remember it, so /my-submissions can answer "where is my item?" after the
    # workspaceitem that carried it has ceased to exist (DESIGN.md §13.3). After
    # the deposit, and never able to fail it: the repository already has the item.
    record_deposit(
        request,
        session,
        instance,
        wsi_id=wsi_id,
        item=submitted_item,
        archived_item=archived_item,
        result=result,
        collection_name=collection,
        collection_uuid=str(live.item.sections.get("collection") or ""),
        title=first_value(patched.sections, TITLE_FIELD) or _submitted_title(submitted),
    )
    # The repository has the file now, so the local copy has no further purpose
    # (DESIGN.md §8).
    release_draft(request, session, wsi_id)
    seeded = _remember_for_next(
        request,
        session,
        instance,
        submitted=submitted,
        collection_uuid=str(live.item.sections.get("collection") or ""),
        collection_name=collection,
    )

    return render(
        request,
        "deposited.html",
        result=result,
        archived_item=archived_item,
        collection_name=collection,
        # What "submit another like this" would carry over, so the offer can say
        # so rather than springing a pre-filled form on somebody (DESIGN.md §13.4).
        seeded_fields=seeded,
        # And what was asked of each file (DESIGN.md §13.5). Named from the values
        # sent, because the item is not read back after a deposit that entered a
        # workflow -- and the answer to "is it embargoed?" must not depend on that.
        # `describe_choices` is bounded by the same files as the patch, so this
        # can never claim a condition the repository was not asked for.
        access_applied=[
            (deposited_names.get(index, ""), description)
            for index, description in sorted(
                describe_choices(access, live.access_options, files=deposited_files).items()
            )
        ],
    )


def _remember_for_next(
    request: Request,
    session: Any,
    instance: Any,
    *,
    submitted: dict[tuple[str, str], list[str]],
    collection_uuid: str,
    collection_name: str,
) -> list[str]:
    """Keep what the next submission could reuse, and say what that is.

    Written after the deposit, from the values the submitter approved rather than
    from the repository's echo of them -- this is "the last thing you sent", not
    "what the item now holds". Returns the metadata fields it kept, which is what
    the confirmation screen names.
    """
    if not request.app.state.settings.defaults_enabled or not collection_uuid:
        return []
    profile = request.app.state.registry.profile_for(instance)
    values = carry_over(submitted, profile)
    if not values:
        session.data.pop("submit_like", None)
        return []
    session.data["submit_like"] = {
        "collection_uuid": collection_uuid,
        "collection_name": collection_name,
        "values": values,
    }
    request.app.state.sessions.save(session)
    return sorted({name.partition("::")[2] for name in values})


@router.get("/review/{wsi_id}/orcid", response_class=HTMLResponse)
async def orcid_suggestions(
    request: Request,
    wsi_id: DraftId,
    field: str = Query(""),
    name: str = Query(""),
    q: str = Query(""),
) -> HTMLResponse:
    """Type-ahead against ORCID's public API (DESIGN.md §13.2).

    Requires a session but asks DSpace nothing: this is a public registry, and
    spending a workspaceitem read on every keystroke to prove the draft exists
    would cost the repository far more than it protects. `orcid.search` never
    raises, so the worst answer is an empty list -- which renders as nothing at
    all, exactly as it should while somebody is still typing.
    """
    require_session(request)
    settings = request.app.state.settings
    # htmx sends the input's value under the input's own *name*
    # (`section::metadata.field`), which is why the row tells us that name; `q`
    # is the plain form, for anything driving this endpoint directly.
    typed = q or (request.query_params.get(name, "") if name else "")
    people = await orcid_lookup.search(
        typed, settings, client=getattr(request.app.state, "orcid_http", None)
    )
    return render(
        request,
        "partials/orcid_results.html",
        people=people,
        field=field,
        query=typed,
        wsi_id=wsi_id,
    )


@router.get("/review/{wsi_id}/vocabulary", response_class=HTMLResponse)
async def vocabulary_suggestions(
    request: Request,
    wsi_id: DraftId,
    vocabulary: str = Query(""),
    field: str = Query(""),
    name: str = Query(""),
    q: str = Query(""),
) -> HTMLResponse:
    """Type-ahead against one of the repository's controlled vocabularies (§13.4).

    Which vocabulary comes from the query string rather than being re-derived
    from the draft's form configuration, and that is a deliberate trade: reading
    it back would cost a workspaceitem read *per keystroke*, and the endpoint it
    reaches is `permitAll` in DSpace -- a signed-in user asking for a vocabulary
    by name learns nothing they could not ask the repository for directly. The
    name is still checked against the shape a vocabulary name can have, because
    it is interpolated into a URL path.

    Like ORCID's, this never fails loudly: a vocabulary that cannot be read
    leaves the box a plain text input, which is what it was before this existed.
    """
    session = require_session(request)
    settings = request.app.state.settings
    typed = (q or (request.query_params.get(name, "") if name else "")).strip()

    entries: list[Any] = []
    unavailable = False
    if (
        settings.vocabulary_typeahead_enabled
        and _VOCABULARY_NAME.fullmatch(vocabulary)
        and len(typed) >= settings.vocabulary_min_query_length
    ):
        async with dspace_client(request, session) as client:
            try:
                entries = await client.vocabulary_matches(
                    vocabulary, typed, size=settings.vocabulary_max_suggestions
                )
            except DSpaceError as exc:
                log.info("vocabulary %s could not be searched: %s", vocabulary, exc)
                unavailable = True

    return render(
        request,
        "partials/vocabulary_results.html",
        entries=entries,
        field=field,
        query=typed,
        unavailable=unavailable,
        min_length=settings.vocabulary_min_query_length,
    )


@router.get("/review/{wsi_id}/access", response_class=HTMLResponse)
async def access_date(
    request: Request,
    wsi_id: DraftId,
    index: int = Query(0, ge=0),
    name: str = Query(""),
    option: str = Query(""),
) -> HTMLResponse:
    """The date box belonging to the access condition just picked (§13.5).

    Which date a condition takes -- a start date, an end date, or none -- is the
    repository's configuration, so the box is re-rendered from it rather than
    shown or hidden by script. An option that takes no date renders nothing,
    which is how choosing open access clears the box.

    The options are read from this draft's own upload configuration (cached),
    not from the query string: unlike a vocabulary name, this decides what the
    form will ask somebody to promise about access. But *only* the options: the
    form configs and vocabularies `_load` would also gather render nothing in
    this fragment, so a picker change costs one workspaceitem read plus cache
    hits, not a re-fetch of everything the review page needs.
    """
    session = require_session(request)
    instance = session_instance(request, session)
    if not _access(request):
        return HTMLResponse("")

    chosen = option or (request.query_params.get(name, "") if name else "")
    cache = request.app.state.form_configs
    async with dspace_client(request, session) as client:
        item = await client.get_workspaceitem(wsi_id)
        sections = await load_sections(client, item, cache=cache, instance_key=instance.key)
        options = parse_options(
            await load_upload_config(client, sections, cache=cache, instance_key=instance.key)
        )

    selected = next((o for o in options if o.name == chosen), None)
    return render(
        request,
        "partials/access_date.html",
        option=selected,
        index=index,
        chosen="",
        date_name=input_name(index, date_input=True),
    )


@router.get("/review/{wsi_id}/row", response_class=HTMLResponse)
async def add_row(
    request: Request,
    wsi_id: DraftId,
    section: str = Query(...),
    field: str = Query(...),
    translation: bool = Query(False),
) -> HTMLResponse:
    """One more empty input for a repeatable field, or for a translation.

    The field's configuration is re-read rather than remembered, so a row can
    never be rendered with a shape the collection does not actually have.

    `translation=1` is the one case where a *non-repeatable* field gains a row
    (DESIGN.md §13.4): a title and its translation are two values of one field,
    distinguished by their language key, and DSpace's own submission form does
    the same. It is still refused unless this instance's profile says that field
    holds the work's own words -- an extra `dc.date.issued` is not a translation.
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

    translatable = _translatable(request, instance)
    match = next((f for f in parse_fields(config) if f.metadata_field == field), None)
    wanted = match is not None and (
        match.repeatable or (translation and match.metadata_field in translatable)
    )
    if not wanted:
        # Nothing to add: an unknown or non-repeatable field gets an empty swap
        # rather than an error page in the middle of a form.
        log.info("ignored add-row for %s/%s on workspaceitem %s", section, field, wsi_id)
        return HTMLResponse("")

    profile = request.app.state.registry.profile_for(instance)
    policy = orcid_lookup.policy_for(
        instance, profile, enabled=request.app.state.settings.orcid_enabled
    )
    options = vocabularies.get(match.vocabulary or "", [])
    orcid = policy.offers_lookup(match.metadata_field)
    form_field = FormField(
        config=match,
        section_id=section,
        values=[FormValue()],
        options=options,
        # An appended author row offers the lookup too, or the second author is
        # the only one who has to type an iD by hand.
        orcid_lookup=orcid,
        # And an appended subject row searches the vocabulary, for the same
        # reason: the help belongs to the field, not to its first row.
        vocabulary_lookup=bool(
            _typeahead(request) and match.vocabulary and not options and not orcid
        ),
        translatable=match.metadata_field in translatable,
    )
    # row_index=None: this row is appended, so it is never the first one and must
    # not repeat the id the field's <label for> points at.
    return render(
        request,
        "partials/field_row.html",
        field=form_field,
        value=FormValue(),
        row_index=None,
        wsi_id=wsi_id,
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
