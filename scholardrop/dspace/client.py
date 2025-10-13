"""The only module that speaks HTTP to DSpace (DESIGN.md §3).

Two cross-cutting mechanics live in `_request` rather than in any endpoint method,
because getting them wrong is the number-one cause of mysterious 403s:

**CSRF.** DSpace issues a token in the `DSPACE-XSRF-TOKEN` response header (and a
matching `DSPACE-XSRF-COOKIE`). Every modifying request must echo the latest one
back in `X-XSRF-TOKEN`. The token rotates on any response -- including the 403
that rejects a stale one -- so we capture it from *every* response and retry a
CSRF failure once with the fresh value.

**Auth.** `POST /api/authn/login` returns a JWT in the `Authorization` response
header; it is sent on every later request and refreshed by POSTing to the same
endpoint with the still-valid token. Credentials are used once and never stored
(DESIGN.md §8) -- so a 401 is recoverable only via token refresh.

Verified against DSpace 9.2 on 2026-07-27: header/cookie names, the CSRF denial
message, `findBySubmitter` requiring the submitter uuid, and deposit answering 204
with no body when an item is archived without review.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from scholardrop.config import Instance
from scholardrop.dspace.errors import (
    DSpaceAuthError,
    DSpaceError,
    DSpaceForbidden,
    DSpaceNotFound,
    DSpaceUnavailable,
    DSpaceValidationError,
    ValidationIssue,
)
from scholardrop.dspace.models import (
    AuthStatus,
    Bitstream,
    Collection,
    CollectionLicense,
    Community,
    DepositResult,
    DSpaceObject,
    EPerson,
    ExternalSource,
    ExternalSourceEntry,
    Item,
    ItemStatus,
    Page,
    VocabularyEntry,
    WorkflowItem,
    WorkspaceItem,
    parse_page,
    parse_search,
)

log = logging.getLogger("scholardrop.dspace")

XSRF_HEADER = "DSPACE-XSRF-TOKEN"
XSRF_COOKIE = "DSPACE-XSRF-COOKIE"
XSRF_REQUEST_HEADER = "X-XSRF-TOKEN"
_MODIFYING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSRF_MARKER = "csrf"


class DSpaceClient:
    """Async client for one DSpace instance, holding one user's session."""

    def __init__(
        self,
        api_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._bearer: str | None = None
        self._xsrf: str | None = None
        self._eperson: EPerson | None = None

    @classmethod
    def for_instance(cls, instance: Instance, **kwargs: Any) -> DSpaceClient:
        return cls(instance.api_url, **kwargs)

    async def __aenter__(self) -> DSpaceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ---- session state -----------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return self._bearer is not None

    @property
    def eperson(self) -> EPerson | None:
        """Whoever last authenticated, as reported by DSpace."""
        return self._eperson

    @property
    def bearer_token(self) -> str | None:
        """The JWT, for storing in the server-side session. Never log or render it."""
        return self._bearer

    @property
    def xsrf_token(self) -> str | None:
        return self._xsrf

    def restore(self, *, bearer: str | None, xsrf: str | None = None) -> None:
        """Re-hydrate from a server-side session across requests.

        Seeds the CSRF *cookie* as well as the token: DSpace compares the
        X-XSRF-TOKEN header against the DSPACE-XSRF-COOKIE cookie, so a rehydrated
        client with an empty cookie jar would fail every modifying request with a
        403 (verified against 9.2 -- header alone is 403, header + cookie passes).
        """
        self._bearer = bearer
        self._xsrf = xsrf
        if xsrf:
            self._sync_csrf_cookie(xsrf)

    def restore_eperson(self, *, uuid: str | None, name: str | None) -> None:
        """Seed the cached EPerson from the session, so log lines are labelled
        without spending a status() round trip on it."""
        if uuid or name:
            self._eperson = EPerson(uuid=uuid or "", name=name or "")

    # ---- authentication ----------------------------------------------------

    async def login(self, user: str, password: str) -> AuthStatus:
        """Exchange credentials for a JWT.

        The password is passed straight through to DSpace and never retained --
        not on this object, not in a log line.
        """
        # A CSRF token is required even for login, so make sure we have one.
        if self._xsrf is None:
            await self._prime_csrf()

        response = await self._request(
            "POST",
            "/authn/login",
            data={"user": user, "password": password},
            auth_retry=False,
            expected=(200,),
            redact_body=True,
        )
        token = _bearer_from_header(response.headers.get("authorization"))
        if not token:
            raise DSpaceAuthError(
                "login succeeded but no bearer token was returned",
                method="POST",
                path="/authn/login",
                status=response.status_code,
            )
        self._bearer = token
        return await self.status()

    async def refresh(self) -> str:
        """Extend the current token's life by POSTing to login with it.

        Raises DSpaceAuthError if there is no token, or DSpace declines.
        """
        if not self._bearer:
            raise DSpaceAuthError("cannot refresh: no bearer token", path="/authn/login")
        response = await self._request("POST", "/authn/login", auth_retry=False, expected=(200,))
        token = _bearer_from_header(response.headers.get("authorization"))
        if not token:
            self._bearer = None
            raise DSpaceAuthError(
                "token refresh returned no bearer token",
                method="POST",
                path="/authn/login",
                status=response.status_code,
            )
        self._bearer = token
        return token

    async def logout(self) -> None:
        """Best-effort server-side invalidation; local state is cleared regardless."""
        if self._bearer:
            try:
                await self._request("POST", "/authn/logout", auth_retry=False, expected=None)
            except DSpaceError as exc:  # logout must never block signing out locally
                log.info("logout call failed, clearing local session anyway: %s", exc)
        self._bearer = None
        self._eperson = None

    async def status(self) -> AuthStatus:
        """Who DSpace thinks we are. Also refreshes the cached EPerson."""
        payload = await self._json("GET", "/authn/status", params={"embed": "eperson"})
        status = AuthStatus.from_payload(payload)
        if status.authenticated and status.eperson is None and status.eperson_link:
            # Older/leaner projections may not embed the eperson; follow the link.
            eperson_payload = await self._json("GET", status.eperson_link)
            status = status.model_copy(update={"eperson": EPerson.from_payload(eperson_payload)})
        self._eperson = status.eperson
        return status

    # ---- community / collection tree (DESIGN.md §6 step 3) -----------------

    async def top_communities(self, page: int = 0, size: int = 20) -> Page[Community]:
        payload = await self._json(
            "GET", "/core/communities/search/top", params={"page": page, "size": size}
        )
        return parse_page(payload, "communities", Community.from_payload)

    async def subcommunities(self, uuid: str, page: int = 0, size: int = 20) -> Page[Community]:
        payload = await self._json(
            "GET", f"/core/communities/{uuid}/subcommunities", params={"page": page, "size": size}
        )
        return parse_page(payload, "subcommunities", Community.from_payload)

    async def collections_of(self, uuid: str, page: int = 0, size: int = 20) -> Page[Collection]:
        payload = await self._json(
            "GET", f"/core/communities/{uuid}/collections", params={"page": page, "size": size}
        )
        return parse_page(payload, "collections", Collection.from_payload)

    async def collection(self, uuid: str) -> Collection:
        payload = await self._json("GET", f"/core/collections/{uuid}")
        return Collection.from_payload(payload)

    async def all_top_communities(self, size: int = 50) -> list[Community]:
        return await self._walk(self.top_communities, size=size)

    async def all_subcommunities(self, uuid: str, size: int = 50) -> list[Community]:
        return await self._walk(self.subcommunities, uuid, size=size)

    async def all_collections_of(self, uuid: str, size: int = 50) -> list[Collection]:
        return await self._walk(self.collections_of, uuid, size=size)

    # ---- authorization (DESIGN.md §4) --------------------------------------

    async def find_submit_authorized(
        self, query: str | None = None, page: int = 0, size: int = 20
    ) -> Page[Collection]:
        """Collections this user may submit to -- filtered by DSpace, not by us."""
        params: dict[str, Any] = {"page": page, "size": size}
        if query:
            params["query"] = query
        payload = await self._json(
            "GET", "/core/collections/search/findSubmitAuthorized", params=params
        )
        return parse_page(payload, "collections", Collection.from_payload)

    async def can_submit(self, collection_self_uri: str) -> bool:
        """Ask DSpace whether this user may submit to a collection.

        A denial is a normal answer (False), never an exception -- the caller
        renders the access-denied screen from it.
        """
        payload = await self._json(
            "GET",
            "/authz/authorizations/search/object",
            params={"uri": collection_self_uri, "feature": "canSubmit"},
        )
        authorizations = parse_page(payload, "authorizations", lambda entry: entry)
        # An empty result has no `_embedded` at all, so trust the items, not `page`.
        return bool(authorizations.items) or authorizations.total > 0

    def collection_uri(self, uuid: str) -> str:
        """Self URI for a collection, for can_submit() when only the uuid is known."""
        return f"{self.api_url}/core/collections/{uuid}"

    # ---- submission lifecycle (DESIGN.md §6 steps 5-7) ---------------------

    async def create_workspaceitem(self, collection_uuid: str) -> WorkspaceItem:
        """Start an in-progress submission in a collection.

        The body is empty, but `Content-Type: application/json` is still required --
        RestResourceController declares `consumes = {"application/json",
        "application/hal+json"}`, and without the header DSpace answers
        415 "Content-Type 'null' is not supported" (verified against 9.2).

        A 403 here is the server-side re-check from DESIGN.md §4 -- it surfaces as
        DSpaceForbidden, which routes treat as denied access rather than an error.
        """
        payload = await self._json(
            "POST",
            "/submission/workspaceitems",
            params={"owningCollection": collection_uuid, "embed": "submissionDefinition"},
            headers={"Content-Type": "application/json"},
            expected=(200, 201),
            resource=f"collection {collection_uuid}",
        )
        return WorkspaceItem.from_payload(payload)

    async def create_workspaceitem_from_entry(
        self, collection_uuid: str, entry_uri: str
    ) -> WorkspaceItem:
        """Start a submission from a registry record the *repository* resolved.

        Same endpoint as `create_workspaceitem`, with the external source entry's
        URI as a `text/uri-list` body. DSpace fetches the record through its own
        live-import provider and maps it with the repository's configuration --
        which is why this is preferred over resolving the registry ourselves
        (DESIGN.md §13.1): a ScholarDrop deposit and a DSpace import of the same
        DOI then land in the same metadata fields.

        Verified against 9.2: `ExternalSourceEntryItemUriListHandler` regex-parses
        the URI out of the *body* and never dereferences it, so a DOI's slash is
        fine here even though the same URI cannot be fetched through Tomcat. A
        record the provider cannot find is a 404 (`ResourceNotFoundException`),
        which surfaces as DSpaceNotFound -- a domain answer, not a failure.
        """
        payload = await self._json(
            "POST",
            "/submission/workspaceitems",
            params={"owningCollection": collection_uuid, "embed": "submissionDefinition"},
            content=entry_uri.encode(),
            headers={"Content-Type": "text/uri-list"},
            expected=(200, 201),
            resource=entry_uri,
        )
        return WorkspaceItem.from_payload(payload)

    async def get_workspaceitem(self, wsi_id: str | int) -> WorkspaceItem:
        payload = await self._json(
            "GET",
            f"/submission/workspaceitems/{wsi_id}",
            params={"embed": "submissionDefinition"},
        )
        return WorkspaceItem.from_payload(payload)

    async def upload_bitstream(
        self,
        wsi_id: str | int,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> WorkspaceItem:
        """Attach a file to the submission's upload section (multipart field `file`)."""
        payload = await self._json(
            "POST",
            f"/submission/workspaceitems/{wsi_id}",
            files={"file": (filename, content, content_type)},
            expected=(200, 201),
            resource=f"workspaceitem {wsi_id}",
        )
        return WorkspaceItem.from_payload(payload)

    async def patch_sections(
        self, wsi_id: str | int, operations: list[dict[str, Any]]
    ) -> WorkspaceItem:
        """Apply JSON Patch operations against /sections/... paths.

        Validation problems come back *in* the returned item's `errors` array
        rather than as a failure, so the review form can flag fields inline. This
        method deliberately does not raise on them.
        """
        payload = await self._json(
            "PATCH",
            f"/submission/workspaceitems/{wsi_id}",
            json=operations,
            headers={"Content-Type": "application/json"},
            expected=(200,),
            resource=f"workspaceitem {wsi_id}",
        )
        return WorkspaceItem.from_payload(payload)

    async def deposit(self, workspaceitem_self_uri: str) -> DepositResult:
        """Hand the submission to the collection's workflow.

        201 + body -> entered review; 204 (no body) -> archived immediately.
        """
        response = await self._request(
            "POST",
            "/workflow/workflowitems",
            content=workspaceitem_self_uri.encode(),
            headers={"Content-Type": "text/uri-list"},
            expected=(200, 201, 204),
            resource=workspaceitem_self_uri,
        )
        if response.status_code == 204 or not response.content.strip():
            return DepositResult(archived=True)
        payload = _decode_json(response)
        return DepositResult(
            archived=False, workflow_item=WorkflowItem.from_payload(payload), raw=payload
        )

    async def find_by_submitter(
        self, eperson_uuid: str | None = None, page: int = 0, size: int = 20
    ) -> Page[WorkspaceItem]:
        """This user's in-progress submissions, for the resume/discard list.

        DSpace requires the submitter uuid (WorkspaceItemRestRepository declares
        `uuid` as required), so we default to the EPerson from the last status()
        call rather than letting the server 400.
        """
        uuid = eperson_uuid or (self._eperson.uuid if self._eperson else None)
        if not uuid:
            raise DSpaceError(
                "find_by_submitter needs an EPerson uuid; call status() first",
                path="/submission/workspaceitems/search/findBySubmitter",
            )
        payload = await self._json(
            "GET",
            "/submission/workspaceitems/search/findBySubmitter",
            params={
                "uuid": uuid,
                "page": page,
                "size": size,
                # Each row says what its draft still needs, and naming the fields
                # means reading the collection's form config -- which is reached
                # through the submission definition. The embed carries the
                # definition without its sections, as everywhere else in 9.2, so
                # `load_sections` still re-reads it (from cache, after the first).
                "embed": "submissionDefinition",
            },
        )
        return parse_page(payload, "workspaceitems", WorkspaceItem.from_payload)

    async def delete_workspaceitem(self, wsi_id: str | int) -> None:
        """Discard a draft (also used to clean up after a failed upload)."""
        await self._request(
            "DELETE",
            f"/submission/workspaceitems/{wsi_id}",
            expected=(200, 204),
            resource=f"workspaceitem {wsi_id}",
        )

    async def collection_license(self, uuid: str) -> CollectionLicense:
        """The deposit licence a submitter has to grant for this collection."""
        payload = await self._json("GET", f"/core/collections/{uuid}/license")
        return CollectionLicense.from_payload(payload)

    async def item(self, uuid_or_href: str) -> DSpaceObject:
        """An archived item -- read after a deposit that skipped review, because
        the handle only exists once the item is in the archive."""
        url = (
            uuid_or_href
            if uuid_or_href.startswith(("http://", "https://"))
            else f"/core/items/{uuid_or_href}"
        )
        return DSpaceObject.from_payload(await self._json("GET", url))

    async def workflowitem(self, wfi_id: str | int) -> WorkflowItem:
        payload = await self._json("GET", f"/workflow/workflowitems/{wfi_id}")
        return WorkflowItem.from_payload(payload)

    async def workflowitem_for_item(self, item_uuid: str) -> WorkflowItem | None:
        """The workflowitem holding this item, or None if it is not in review.

        **This is the search a submitter is allowed to run.** The obvious
        endpoint, `workflowitems/search/findBySubmitter`, is annotated
        `@PreAuthorize("hasAuthority('ADMIN')")` -- it answers 200 for a
        repository administrator and 403 for everybody else, so a page built on
        it works for whoever developed it and for nobody who uses it.
        `search/item` authorizes on READ of the item instead, which the submitter
        of that item has.

        **Nothing found is 204, not 404** (the repository method returns null),
        so this cannot go through `_json`: an empty body is the answer, not a
        malformed one.

        A uuid no item has answers **500**, not 404 -- `itemService.find` returns
        null and `findByItem` is handed it -- so only ask about an item that has
        just resolved.
        """
        response = await self._request(
            "GET",
            "/workflow/workflowitems/search/item",
            params={"uuid": item_uuid},
            expected=(200, 204),
            resource=f"item {item_uuid}",
        )
        if response.status_code == 204 or not response.content.strip():
            return None
        return WorkflowItem.from_payload(_decode_json(response))

    async def item_status(self, item_uuid: str) -> ItemStatus:
        """Where a deposited item has got to: archived, in review, or back as a draft.

        One read in the common case. `/core/items/{uuid}` carries `inArchive` and
        the handle, and an archived item needs nothing further -- the workflow
        search only runs for an item that is *not* archived, which is the
        minority and the only case where the answer is ambiguous.

        A missing item is `found=False` rather than an exception: a submission
        removed from the repository is a row on a status page, not an error page.
        Anything else that goes wrong is left to the caller, which decides
        per row whether one unreadable submission takes the list down (it must
        not).
        """
        if not item_uuid:
            return ItemStatus.unchecked()
        try:
            payload = await self._json("GET", f"/core/items/{item_uuid}")
        except DSpaceNotFound:
            return ItemStatus.gone(item_uuid)
        status = ItemStatus.from_item(payload)
        if status.in_archive or status.withdrawn:
            return status
        workflow = await self.workflowitem_for_item(item_uuid)
        if workflow is not None:
            return status.model_copy(update={"in_workflow": True, "workflow_id": workflow.id})
        # Neither archived nor in review: the submission has come back to its
        # submitter. Finding which draft it is now is what makes the row
        # resumable -- the id in the log names a workspaceitem the deposit
        # consumed, and a rejection mints a new one.
        draft = await self.workspaceitem_for_item(item_uuid)
        if draft is None:
            return status
        return status.model_copy(update={"workspaceitem_id": draft.id})

    async def workspaceitem_for_item(self, item_uuid: str) -> WorkspaceItem | None:
        """The draft holding this item, or None. Same 204-means-nothing shape as
        `workflowitem_for_item`, and authorized the same way (READ on the item)."""
        response = await self._request(
            "GET",
            "/submission/workspaceitems/search/item",
            params={"uuid": item_uuid},
            expected=(200, 204),
            resource=f"item {item_uuid}",
        )
        if response.status_code == 204 or not response.content.strip():
            return None
        return WorkspaceItem.from_payload(_decode_json(response))

    # ---- discovery and files (DESIGN.md §12) -------------------------------

    async def search_items(
        self,
        query: str | None = None,
        *,
        page: int = 0,
        size: int = 20,
        sort: str | None = None,
        embed: str | None = None,
        configuration: str | None = None,
    ) -> Page[Item]:
        """Discovery search, scoped to items.

        Results are filtered by DSpace to what this client may see, so an
        unauthenticated crawl indexes only public items -- which is exactly what
        the shared duplicate index is allowed to hold (DESIGN.md §12).

        `sort` is `field,DESC`. Only *configured* sort fields are accepted: the
        default configuration offers score, title, `dc.date.issued` and
        `dc.date.accessioned`, and `lastModified` answers **422 Invalid search
        request** despite being a field on every item (verified against 9.2).
        """
        params: dict[str, Any] = {"dsoType": "item", "page": page, "size": size}
        if query:
            params["query"] = query
        if sort:
            params["sort"] = sort
        if embed:
            params["embed"] = embed
        if configuration:
            params["configuration"] = configuration
        payload = await self._json("GET", "/discover/search/objects", params=params)
        return parse_search(payload, Item.from_payload)

    async def bundles_of(self, item_uuid: str, page: int = 0, size: int = 20) -> Page[DSpaceObject]:
        payload = await self._json(
            "GET", f"/core/items/{item_uuid}/bundles", params={"page": page, "size": size}
        )
        return parse_page(payload, "bundles", DSpaceObject.from_payload)

    async def bitstreams_of(
        self, bundle_uuid: str, page: int = 0, size: int = 50
    ) -> Page[Bitstream]:
        payload = await self._json(
            "GET", f"/core/bundles/{bundle_uuid}/bitstreams", params={"page": page, "size": size}
        )
        return parse_page(payload, "bitstreams", Bitstream.from_payload)

    async def bitstreams_in_bundle(self, item_uuid: str, bundle_name: str) -> list[Bitstream]:
        """Every bitstream of one named bundle, the slow way.

        The fallback for when `embed=bundles/bitstreams` did not arrive or came
        back truncated -- three requests instead of nothing, so it is only worth
        paying per item that needs it.
        """
        found: list[Bitstream] = []
        for bundle in await self._walk(self.bundles_of, item_uuid, size=20):
            if bundle.name != bundle_name:
                continue
            for bitstream in await self._walk(self.bitstreams_of, bundle.uuid, size=50):
                found.append(bitstream.model_copy(update={"bundle_name": bundle.name}))
        return found

    # ---- submission configuration (DESIGN.md §3, "read it at runtime") -----

    # ---- external sources (DESIGN.md §13.1) --------------------------------

    async def list_external_sources(self, size: int = 100) -> list[ExternalSource]:
        """Which live-import providers this repository has configured.

        Read once per instance and cached: it is repository configuration, and
        changing it means restarting DSpace. A stock 9.2 lists about thirty.
        """
        payload = await self._json(
            "GET", "/integration/externalsources", params={"size": size, "page": 0}
        )
        page = parse_page(payload, "externalsources", ExternalSource.from_payload)
        return page.items

    async def external_entry(self, source_id: str, entry_id: str) -> ExternalSourceEntry:
        """One record from a provider, as the repository would import it.

        Only usable for identifiers with no slash in them. Verified against 9.2:
        Tomcat refuses an encoded slash in a path segment before Spring sees it
        ("Invalid URI: noSlash"), and an unencoded one does not match the route --
        so every DOI is unreadable here, which is exactly why
        `create_workspaceitem_from_entry` builds the URI rather than fetching it.

        A provider that has no such record answers **400**, not 404
        (`LiveImportDataProvider.getExternalDataObject` -> "No record found"), so
        both are mapped to DSpaceNotFound for the caller.
        """
        try:
            payload = await self._json(
                "GET",
                f"/integration/externalsources/{source_id}/entryValues/{entry_id}",
                resource=f"{source_id} entry {entry_id}",
            )
        except DSpaceError as exc:
            if exc.status == 400:
                raise DSpaceNotFound(
                    f"{source_id} has no record for {entry_id}",
                    method="GET",
                    path=f"/integration/externalsources/{source_id}/entryValues/{entry_id}",
                    status=400,
                ) from exc
            raise
        return ExternalSourceEntry.from_payload(payload)

    async def submission_definition(self, name: str) -> dict[str, Any]:
        """A submission definition *with its sections embedded*.

        The workspaceitem's own `embed=submissionDefinition` gives the definition
        without them -- verified against 9.2, where the embedded object is just
        `{id, name, isDefault, _links}` -- and the `/sections` sub-resource
        answers 204. `?embed=sections` on the definition itself is what actually
        returns the panels, so that is the call the form builder needs.
        """
        url = (
            name
            if name.startswith(("http://", "https://"))
            else f"/config/submissiondefinitions/{name}"
        )
        return await self._json("GET", url, params={"embed": "sections"})

    async def submission_form(self, name: str) -> dict[str, Any]:
        """The field configuration of one `submission-form` section.

        Takes either the form's name or the `config` href the submission
        definition carries -- the definition is the honest source of which form a
        section uses, and it gives a URL rather than a name.

        Returned raw: this is a configuration document (rows of fields with their
        input types, labels, and selectable metadata), and the form builder in
        `scholardrop.submission.form` is the thing that understands its shape.
        The submission *definition* only links here -- it does not embed it.
        """
        url = (
            name if name.startswith(("http://", "https://")) else f"/config/submissionforms/{name}"
        )
        return await self._json("GET", url)

    async def vocabulary_entries(
        self, name: str, page: int = 0, size: int = 100
    ) -> Page[VocabularyEntry]:
        payload = await self._json(
            "GET",
            f"/submission/vocabularies/{name}/entries",
            params={"page": page, "size": size},
        )
        return parse_page(payload, "entries", VocabularyEntry.from_payload)

    async def all_vocabulary_entries(self, name: str, size: int = 100) -> list[VocabularyEntry]:
        """Every option of a controlled vocabulary, for a dropdown's <select>."""
        return await self._walk(self.vocabulary_entries, name, size=size)

    # ---- plumbing ----------------------------------------------------------

    async def _walk(self, method: Any, *args: Any, size: int) -> list[Any]:
        """Collect every page of a paginated endpoint."""
        results: list[Any] = []
        page_number = 0
        while True:
            page = await method(*args, page=page_number, size=size)
            results.extend(page.items)
            if not page.has_next or not page.items:
                return results
            page_number += 1

    async def _prime_csrf(self) -> None:
        """Fetch an initial CSRF token. Cheap, and login cannot proceed without one."""
        await self._request("GET", "/authn/status", expected=None)

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(method, url, **kwargs)
        return _decode_json(response)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        expected: tuple[int, ...] | None = (200,),
        auth_retry: bool = True,
        csrf_retry: bool = True,
        resource: str | None = None,
        redact_body: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one request, handling CSRF and token refresh.

        `expected` is advisory: any 2xx is accepted, and it exists so a caller can
        document which codes it actually handles. `None` means "don't check".
        """
        target = self._absolute(url)
        headers = dict(kwargs.pop("headers", None) or {})
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if method.upper() in _MODIFYING_METHODS and self._xsrf:
            headers[XSRF_REQUEST_HEADER] = self._xsrf

        try:
            response = await self._http.request(method, target, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise DSpaceUnavailable(
                f"could not reach DSpace: {exc}", method=method, path=_path_of(target)
            ) from exc

        self._capture_csrf(response)
        self._log(method, target, response, redact_body=redact_body)

        if response.status_code == 401 and auth_retry and self._bearer:
            log.info("token rejected; refreshing once and retrying %s %s", method, _path_of(target))
            await self.refresh()
            return await self._request(
                method,
                url,
                expected=expected,
                auth_retry=False,
                csrf_retry=csrf_retry,
                resource=resource,
                redact_body=redact_body,
                **kwargs,
            )

        if response.status_code == 403 and csrf_retry and _looks_like_csrf_failure(response):
            # The rejecting response carries a fresh token, already captured above.
            log.info("CSRF token rejected; resyncing and retrying %s %s", method, _path_of(target))
            if self._xsrf is None:
                await self._prime_csrf()
            return await self._request(
                method,
                url,
                expected=expected,
                auth_retry=auth_retry,
                csrf_retry=False,
                resource=resource,
                redact_body=redact_body,
                **kwargs,
            )

        self._raise_for_status(method, target, response, resource=resource)

        if expected is not None and response.status_code not in expected:
            log.debug(
                "%s %s returned %d, handler documented %s",
                method,
                _path_of(target),
                response.status_code,
                expected,
            )
        return response

    def _absolute(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return urljoin(self.api_url + "/", url.lstrip("/"))

    def _capture_csrf(self, response: httpx.Response) -> None:
        """Always store the newest token -- it rotates on any response."""
        token = response.headers.get(XSRF_HEADER) or response.cookies.get(XSRF_COOKIE)
        if token:
            self._xsrf = token
            # Keep the jar in step with the header. DSpace normally sends both, and
            # httpx would store the Set-Cookie itself, but a header that rotates
            # without a matching Set-Cookie would silently desync the pair.
            self._sync_csrf_cookie(token)

    def _sync_csrf_cookie(self, token: str) -> None:
        # Empty domain => sent to whatever host this client talks to, which is one
        # instance. Set on the jar rather than per-request so it survives redirects.
        self._http.cookies.set(XSRF_COOKIE, token)

    def _log(self, method: str, url: str, response: httpx.Response, *, redact_body: bool) -> None:
        # Method, path, status, EPerson only -- never credentials or tokens
        # (DESIGN.md §7). `redact_body` marks calls whose payload holds a password;
        # bodies are not logged at all, so it is documentation plus a guard.
        who = self._eperson.display_name if self._eperson else "anonymous"
        log.info(
            "%s %s -> %d [%s]%s",
            method.upper(),
            _path_of(url),
            response.status_code,
            who,
            " (body redacted)" if redact_body else "",
        )

    def _raise_for_status(
        self, method: str, url: str, response: httpx.Response, *, resource: str | None
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        path = _path_of(url)
        detail = _error_message(response)

        if status == 401:
            self._bearer = None
            raise DSpaceAuthError(
                "not authenticated (session expired or credentials rejected)",
                method=method,
                path=path,
                status=status,
            )
        if status == 403:
            raise DSpaceForbidden(
                detail or "DSpace denied this operation",
                method=method,
                path=path,
                status=status,
                resource=resource,
                detail=detail,
            )
        if status == 404:
            raise DSpaceNotFound(
                detail or "that no longer exists in the repository",
                method=method,
                path=path,
                status=status,
            )
        if status in (400, 422):
            issues = _validation_issues(response)
            if issues:
                raise DSpaceValidationError(
                    detail or "DSpace rejected the submission",
                    issues=issues,
                    method=method,
                    path=path,
                    status=status,
                )
        if status >= 500:
            raise DSpaceUnavailable(
                detail or "DSpace returned a server error",
                method=method,
                path=path,
                status=status,
            )
        raise DSpaceError(
            detail or "unexpected DSpace response", method=method, path=path, status=status
        )


def _bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return value.strip() or None


def _looks_like_csrf_failure(response: httpx.Response) -> bool:
    """DSpace says 'Access is denied. Invalid CSRF token.' -- verified on 9.2."""
    message = _error_message(response) or ""
    return _CSRF_MARKER in message.lower()


def _error_message(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200] or None
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _validation_issues(response: httpx.Response) -> list[ValidationIssue]:
    try:
        payload = response.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("errors") or []
    if not isinstance(raw, list):
        return []
    return [ValidationIssue.from_payload(e) for e in raw if isinstance(e, dict)]


def _decode_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DSpaceError(
            "expected JSON from DSpace",
            method=response.request.method if response.request else None,
            path=_path_of(str(response.url)),
            status=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise DSpaceError(
            f"expected a JSON object from DSpace, got {type(payload).__name__}",
            path=_path_of(str(response.url)),
            status=response.status_code,
        )
    return payload


def _path_of(url: str) -> str:
    return httpx.URL(url).path or url
