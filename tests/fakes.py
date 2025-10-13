"""A stand-in DSpaceClient for route tests.

Only the surface the routes touch, with recorded calls so tests can assert that a
route asked DSpace the right question -- notably that the canSubmit gate runs for a
pick from "Browse all" and is skipped for one from "My collections".

The submission fixtures below are trimmed copies of what DSpace 9.2 actually
returns for the shipped `traditional` definition (captured 2026-07-28), so a form
test fails when our reading of that shape is wrong rather than when a hand-made
fixture disagrees with itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

from scholardrop.dspace import (
    Collection,
    CollectionLicense,
    Community,
    DepositResult,
    DSpaceAuthError,
    DSpaceNotFound,
    EPerson,
    ExternalSource,
    ExternalSourceEntry,
    Page,
    PageInfo,
    VocabularyEntry,
    WorkflowItem,
)
from scholardrop.dspace.models import AuthStatus, Item, ItemStatus, WorkspaceItem
from scholardrop.extract.mapper import map_metadata
from scholardrop.extract.models import ExtractedMetadata, SourceTier
from scholardrop.submission.jobs import ExtractionResult


def page_of(items: list[Any], *, number: int = 0, size: int = 20, total: int | None = None) -> Page:
    total = len(items) if total is None else total
    total_pages = 0 if total == 0 else (total + size - 1) // size
    return Page(
        items=items,
        info=PageInfo(number=number, size=size, totalElements=total, totalPages=total_pages),
    )


def make_collection(uuid: str, name: str, handle: str | None = None) -> Collection:
    return Collection(
        uuid=uuid,
        name=name,
        handle=handle or f"11244/{uuid}",
        self_link=f"https://dspace.test/server/api/core/collections/{uuid}",
    )


def make_community(uuid: str, name: str) -> Community:
    return Community(
        uuid=uuid,
        name=name,
        handle=f"11244/{uuid}",
        self_link=f"https://dspace.test/server/api/core/communities/{uuid}",
    )


API = "https://dspace.test/server/api"


def _field(
    label: str,
    metadata: str,
    *,
    input_type: str = "onebox",
    mandatory: bool = False,
    repeatable: bool = False,
    vocabulary: str | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    selectable = [{"metadata": metadata, "label": None, "closed": bool(vocabulary)}]
    if vocabulary:
        selectable[0]["controlledVocabulary"] = vocabulary
    for sub_label, sub_metadata in extra or []:
        selectable.append({"metadata": sub_metadata, "label": sub_label, "closed": False})
    return {
        "input": {"type": input_type},
        "label": label,
        "mandatory": mandatory,
        "repeatable": repeatable,
        "hints": f"Enter the {label.lower()}.",
        "mandatoryMessage": f"You must enter {label.lower()}." if mandatory else "",
        "selectableMetadata": selectable,
    }


FORM_PAGE_ONE = {
    "id": "traditionalpageone",
    "name": "traditionalpageone",
    "rows": [
        {"fields": [_field("Author", "dc.contributor.author", repeatable=True)]},
        {"fields": [_field("Title", "dc.title", mandatory=True)]},
        {"fields": [_field("Date of Issue", "dc.date.issued", input_type="date", mandatory=True)]},
        {
            "fields": [
                _field(
                    "Type",
                    "dc.type",
                    input_type="dropdown",
                    mandatory=True,
                    repeatable=True,
                    vocabulary="common_types",
                )
            ]
        },
        {
            "fields": [
                _field(
                    "Identifiers",
                    "dc.identifier.issn",
                    repeatable=True,
                    extra=[("Other", "dc.identifier.other")],
                )
            ]
        },
    ],
}

FORM_PAGE_TWO = {
    "id": "traditionalpagetwo",
    "name": "traditionalpagetwo",
    "rows": [
        {"fields": [_field("Subject Keywords", "dc.subject", input_type="tag", repeatable=True)]},
        {"fields": [_field("Abstract", "dc.description.abstract", input_type="textarea")]},
    ],
}

SUBMISSION_FORMS = {"traditionalpageone": FORM_PAGE_ONE, "traditionalpagetwo": FORM_PAGE_TWO}

LICENCE_TEXT = (
    "NON-EXCLUSIVE DISTRIBUTION LICENSE\n\nBy signing and submitting this license, "
    "you (the author) grant the repository the non-exclusive right to reproduce and "
    "distribute your submission."
)

VOCABULARIES = {
    "common_types": [
        VocabularyEntry(value="Article", display="Article"),
        VocabularyEntry(value="Thesis", display="Thesis"),
    ]
}


def _definition_section(section_id: str, section_type: str, header: str | None = None) -> dict:
    panel: dict[str, Any] = {
        "id": section_id,
        "sectionType": section_type,
        "mandatory": True,
        "_links": {"self": {"href": f"{API}/config/submissionsections/{section_id}"}},
    }
    if header:
        panel["header"] = header
    if section_type == "submission-form":
        panel["_links"]["config"] = {"href": f"{API}/config/submissionforms/{section_id}"}
    return panel


DEFINITION_SECTIONS = [
    _definition_section("collection", "collection"),
    _definition_section(
        "traditionalpageone", "submission-form", "submit.progressbar.describe.stepone"
    ),
    _definition_section(
        "traditionalpagetwo", "submission-form", "submit.progressbar.describe.steptwo"
    ),
    _definition_section("upload", "upload", "submit.progressbar.upload"),
    _definition_section("license", "license", "submit.progressbar.license"),
]

# What /config/submissiondefinitions/traditional?embed=sections answers.
TRADITIONAL_DEFINITION = {
    "id": "traditional",
    "name": "traditional",
    "isDefault": True,
    "_links": {"self": {"href": f"{API}/config/submissiondefinitions/traditional"}},
    "_embedded": {"sections": {"_embedded": {"sections": DEFINITION_SECTIONS}}},
}

# What a *workspaceitem* embeds: the definition without its panels. Getting this
# wrong is exactly what the Phase 4 gate caught.
LEAN_DEFINITION = {
    "id": "traditional",
    "name": "traditional",
    "isDefault": True,
    "_links": {
        "self": {"href": f"{API}/config/submissiondefinitions/traditional"},
        "sections": {"href": f"{API}/config/submissiondefinitions/traditional/sections"},
    },
}


def workspaceitem_payload(
    wsi_id: str = "42",
    *,
    collection_uuid: str = "c1",
    sections: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A workspaceitem as DSpace 9.2 returns it, definition embedded."""
    data: dict[str, Any] = {
        "collection": collection_uuid,
        "traditionalpageone": {},
        "traditionalpagetwo": {},
        "upload": {"primary": None, "files": files or []},
        "license": {"url": None, "acceptanceDate": None, "granted": False},
    }
    data.update(sections or {})
    return {
        "id": wsi_id,
        "lastModified": "2026-07-28T06:37:16.538Z",
        "sections": data,
        "errors": errors
        if errors is not None
        else [
            {
                "message": "error.validation.required",
                "paths": [
                    "/sections/traditionalpageone/dc.title",
                    "/sections/traditionalpageone/dc.date.issued",
                    "/sections/traditionalpageone/dc.type",
                ],
            }
        ],
        # Every workspaceitem link is a *sub-resource* of the workspaceitem --
        # `item` is `/submission/workspaceitems/{id}/item`, not `/core/items/{uuid}`
        # (checked against 9.2, 2026-07-30). A fixture that pointed at /core/items
        # made it look as though the item uuid could be read off the link; it
        # cannot, and the link has to be followed.
        "_links": {
            "self": {"href": f"{API}/submission/workspaceitems/{wsi_id}"},
            "item": {"href": f"{API}/submission/workspaceitems/{wsi_id}/item"},
        },
        "_embedded": {"submissionDefinition": LEAN_DEFINITION},
    }


def make_workspaceitem(wsi_id: str = "42", **kwargs: Any) -> WorkspaceItem:
    return WorkspaceItem.from_payload(workspaceitem_payload(wsi_id, **kwargs))


def upload_file(filename: str, size_bytes: int = 1024, uuid: str = "bs-1") -> dict[str, Any]:
    """One entry of the upload section's `files`, shaped as DSpace 9.2 sends it.

    Note what is *not* here: a `name` key. `UploadBitstreamRest` has none -- the
    filename is `dc.title` in the bitstream's metadata (confirmed against a live
    draft, 2026-07-30). A fixture that invents one hides the bug it should catch.
    """
    return {
        "uuid": uuid,
        "sizeBytes": size_bytes,
        "metadata": {
            "dc.title": [{"value": filename, "language": None, "place": 0}],
            "dc.source": [{"value": filename, "language": None, "place": 0}],
        },
        "accessConditions": [],
        "format": {"mimetype": "application/pdf", "shortDescription": "Adobe PDF"},
    }


def metadata_values(*values: str) -> list[dict[str, Any]]:
    """Section data shape: [{"value": "..."}, ...]."""
    return [{"value": v, "language": None, "place": i} for i, v in enumerate(values)]


class FakeExtractor:
    """Stands in for the PDF pipeline (app.state.extractor).

    `gate` holds a job in its running state so the progress/poll behaviour can be
    tested without timing luck.
    """

    def __init__(self) -> None:
        self.metadata = ExtractedMetadata()
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.registry_notice: str | None = None
        self.calls: list[Any] = []

    def __call__(self, path: Any = None, **kwargs: Any) -> Any:
        self.calls.append((path, kwargs))
        return self._run(kwargs["profile"])

    @property
    def last_call(self) -> tuple[Any, dict[str, Any]]:
        if not self.calls:
            raise AssertionError("the extractor was never called")
        return self.calls[-1]

    async def _run(self, profile: Any) -> ExtractionResult:
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return ExtractionResult(
            metadata=self.metadata,
            mapped=map_metadata(self.metadata, profile),
            registry_notice=self.registry_notice,
            registry_used=self.metadata.tier_used == SourceTier.REGISTRY,
        )


class FakeDSpaceClient:
    """Scriptable fake. Set the attributes a test cares about; ignore the rest."""

    def __init__(self, instance: Any = None) -> None:
        self.instance = instance
        self.api_url = "https://dspace.test/server/api"

        # Scripted responses
        self.login_ok = True
        self.login_error: Exception | None = None
        self.authenticated_after_login = True
        self.eperson_name = "Jane Doe"
        self.eperson_uuid = "eperson-1"
        self.authorized_collections: list[Collection] = []
        self.communities: list[Community] = []
        self.subcommunity_map: dict[str, list[Community]] = {}
        self.collection_map: dict[str, list[Collection]] = {}
        self.collections_by_uuid: dict[str, Collection] = {}
        self.can_submit_result = True
        self.raise_on: dict[str, Exception] = {}

        # Submission surface (phase 4)
        self.workspaceitems: dict[str, WorkspaceItem] = {}
        self.next_workspaceitem_id = "42"
        self.definition: dict[str, Any] = TRADITIONAL_DEFINITION
        self.submission_forms: dict[str, dict[str, Any]] = dict(SUBMISSION_FORMS)
        self.vocabularies: dict[str, list[VocabularyEntry]] = dict(VOCABULARIES)
        self.deleted: list[str] = []

        # Deposit (phase 5)
        self.licence = CollectionLicense(text=LICENCE_TEXT, custom=False)
        self.patches: list[list[dict[str, Any]]] = []
        # What patch_sections answers with: by default a clean item (no errors).
        self.patched: WorkspaceItem | None = None
        self.errors_after_patch: list[dict[str, Any]] = []
        self.deposit_result = DepositResult(
            archived=False, workflow_item=WorkflowItem(id="9001", self_link=f"{API}/workflow/9001")
        )
        self.archived_item = Collection(
            uuid="item-1", name="A Deposited Thesis", handle="11244/9999"
        )

        # External sources / identifier-first submission (phase 8). `sources` is
        # what the instance lists; `resolving_sources` is the subset that
        # actually has the record -- the rest answer 404, as a real provider does
        # when asked for the wrong kind of identifier.
        self.external_sources: list[ExternalSource] = []
        self.resolving_sources: set[str] = set()
        self.external_entries: dict[tuple[str, str], ExternalSourceEntry] = {}
        # What a draft created from a registry entry already holds.
        self.entry_sections: dict[str, Any] = {
            "traditionalpageone": {"dc.title": metadata_values("A Published Article")}
        }

        # Duplicate detection (phase 4c). `search_items` answers with whatever is
        # in `search_results`; `missing_items` is how a test makes an indexed hit
        # turn out to be gone from the repository.
        self.search_results: list[Item] = []
        self.items_by_uuid: dict[str, Any] = {}
        self.missing_items: set[str] = set()

        # Submission status (phase 10). A uuid with no scripted status answers
        # "archived", which is what most deposits on this VM actually become.
        self.item_statuses: dict[str, ItemStatus] = {}
        self.default_item_status = ItemStatus(
            uuid="item-1", found=True, in_archive=True, handle="11244/9999"
        )

        # Recorded interactions
        self.calls: list[tuple[str, tuple, dict]] = []
        self.closed = False
        self._bearer: str | None = None
        self._xsrf: str | None = None
        self._eperson: EPerson | None = None

    # -- bookkeeping the routes rely on ----------------------------------

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.raise_on:
            raise self.raise_on[name]

    def called(self, name: str) -> bool:
        return any(call[0] == name for call in self.calls)

    def call_args(self, name: str) -> tuple[tuple, dict]:
        for called, args, kwargs in self.calls:
            if called == name:
                return args, kwargs
        raise AssertionError(f"{name} was not called; calls: {[c[0] for c in self.calls]}")

    @property
    def bearer_token(self) -> str | None:
        return self._bearer

    @property
    def xsrf_token(self) -> str | None:
        return self._xsrf

    @property
    def eperson(self) -> EPerson | None:
        return self._eperson

    def restore(self, *, bearer: str | None, xsrf: str | None = None) -> None:
        self._record("restore", bearer=bearer, xsrf=xsrf)
        self._bearer = bearer
        self._xsrf = xsrf

    def restore_eperson(self, *, uuid: str | None, name: str | None) -> None:
        if uuid or name:
            self._eperson = EPerson(uuid=uuid or "", name=name or "")

    async def aclose(self) -> None:
        self.closed = True

    # -- API surface ------------------------------------------------------

    async def login(self, user: str, password: str) -> AuthStatus:
        self._record("login", user=user)
        if self.login_error is not None:
            raise self.login_error
        if not self.login_ok:
            raise DSpaceAuthError("credentials rejected", path="/authn/login")
        self._bearer = "jwt-from-login"
        self._xsrf = "xsrf-from-login"
        eperson = EPerson(uuid=self.eperson_uuid, name=self.eperson_name)
        self._eperson = eperson
        return AuthStatus(
            authenticated=self.authenticated_after_login,
            auth_method="password",
            eperson=eperson,
        )

    async def logout(self) -> None:
        self._record("logout")
        self._bearer = None

    async def status(self) -> AuthStatus:
        self._record("status")
        return AuthStatus(
            authenticated=self._bearer is not None,
            eperson=EPerson(uuid=self.eperson_uuid, name=self.eperson_name),
        )

    async def find_submit_authorized(
        self, query: str | None = None, page: int = 0, size: int = 20
    ) -> Page:
        self._record("find_submit_authorized", query=query, page=page, size=size)
        items = self.authorized_collections
        if query:
            items = [c for c in items if query.lower() in c.name.lower()]
        return page_of(items, number=page, size=size)

    async def top_communities(self, page: int = 0, size: int = 20) -> Page:
        self._record("top_communities", page=page, size=size)
        return page_of(self.communities, number=page, size=size)

    async def subcommunities(self, uuid: str, page: int = 0, size: int = 20) -> Page:
        self._record("subcommunities", uuid, page=page, size=size)
        return page_of(self.subcommunity_map.get(uuid, []), number=page, size=size)

    async def collections_of(self, uuid: str, page: int = 0, size: int = 20) -> Page:
        self._record("collections_of", uuid, page=page, size=size)
        return page_of(self.collection_map.get(uuid, []), number=page, size=size)

    async def collection(self, uuid: str) -> Collection:
        self._record("collection", uuid)
        return self.collections_by_uuid.get(uuid) or make_collection(uuid, f"Collection {uuid}")

    async def can_submit(self, collection_self_uri: str) -> bool:
        self._record("can_submit", collection_self_uri)
        return self.can_submit_result

    def collection_uri(self, uuid: str) -> str:
        return f"{self.api_url}/core/collections/{uuid}"

    # -- submission (phase 4) ---------------------------------------------

    async def create_workspaceitem(self, collection_uuid: str) -> WorkspaceItem:
        self._record("create_workspaceitem", collection_uuid)
        item = make_workspaceitem(self.next_workspaceitem_id, collection_uuid=collection_uuid)
        self.workspaceitems[item.id] = item
        return item

    async def get_workspaceitem(self, wsi_id: str | int) -> WorkspaceItem:
        self._record("get_workspaceitem", str(wsi_id))
        return self.workspaceitems.get(str(wsi_id)) or make_workspaceitem(str(wsi_id))

    # -- external sources (phase 8) ---------------------------------------

    async def list_external_sources(self, size: int = 100) -> list[ExternalSource]:
        self._record("list_external_sources", size=size)
        return list(self.external_sources)

    async def external_entry(self, source_id: str, entry_id: str) -> ExternalSourceEntry:
        self._record("external_entry", source_id, entry_id)
        entry = self.external_entries.get((source_id, entry_id))
        if entry is None:
            raise DSpaceNotFound(f"{source_id} has no record for {entry_id}")
        return entry

    async def create_workspaceitem_from_entry(
        self, collection_uuid: str, entry_uri: str
    ) -> WorkspaceItem:
        self._record("create_workspaceitem_from_entry", collection_uuid, entry_uri)
        # Keyed by provider id, the way the real failure works: a provider that
        # does not have the record answers 404 and creates nothing.
        source_id = entry_uri.split("/externalsources/", 1)[-1].split("/", 1)[0]
        if source_id not in self.resolving_sources:
            raise DSpaceNotFound(f"{source_id} has no record for {entry_uri}")
        item = make_workspaceitem(
            self.next_workspaceitem_id,
            collection_uuid=collection_uuid,
            sections=self.entry_sections,
        )
        self.workspaceitems[item.id] = item
        return item

    async def upload_bitstream(
        self,
        wsi_id: str | int,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> WorkspaceItem:
        self._record("upload_bitstream", str(wsi_id), filename=filename, size=len(content))
        item = self.workspaceitems.get(str(wsi_id)) or make_workspaceitem(str(wsi_id))
        item = make_workspaceitem(item.id, files=[upload_file(filename, len(content))])
        self.workspaceitems[item.id] = item
        return item

    async def delete_workspaceitem(self, wsi_id: str | int) -> None:
        self._record("delete_workspaceitem", str(wsi_id))
        self.deleted.append(str(wsi_id))
        self.workspaceitems.pop(str(wsi_id), None)

    async def find_by_submitter(
        self, eperson_uuid: str | None = None, page: int = 0, size: int = 20
    ) -> Page:
        self._record("find_by_submitter", eperson_uuid, page=page, size=size)
        return page_of(list(self.workspaceitems.values()), number=page, size=size)

    async def submission_definition(self, name: str) -> dict[str, Any]:
        self._record("submission_definition", str(name).rstrip("/").rsplit("/", 1)[-1])
        return self.definition

    async def submission_form(self, name: str) -> dict[str, Any]:
        # Routes pass the config href from the definition; key on its last segment.
        key = str(name).rstrip("/").rsplit("/", 1)[-1]
        self._record("submission_form", key)
        return self.submission_forms.get(key, {"id": key, "rows": []})

    async def all_vocabulary_entries(self, name: str, size: int = 100) -> list[VocabularyEntry]:
        self._record("all_vocabulary_entries", name)
        return self.vocabularies.get(name, [])

    # -- deposit (phase 5) -------------------------------------------------

    async def collection_license(self, uuid: str) -> CollectionLicense:
        self._record("collection_license", uuid)
        return self.licence

    async def patch_sections(self, wsi_id: str | int, operations: list[dict[str, Any]]):
        self._record("patch_sections", str(wsi_id), operations=operations)
        self.patches.append(operations)
        item = self.workspaceitems.get(str(wsi_id)) or make_workspaceitem(str(wsi_id))
        if self.patched is not None:
            return self.patched
        # Mirror DSpace: validation problems come back *in* the item, not as a
        # failure, and granting the licence clears its error.
        return make_workspaceitem(item.id, errors=self.errors_after_patch)

    async def deposit(self, workspaceitem_self_uri: str) -> DepositResult:
        self._record("deposit", workspaceitem_self_uri)
        return self.deposit_result

    async def item(self, uuid_or_href: str) -> Collection:
        self._record("item", uuid_or_href)
        if uuid_or_href in self.missing_items:
            raise DSpaceNotFound("gone", path=f"/core/items/{uuid_or_href}")
        return self.items_by_uuid.get(uuid_or_href) or self.archived_item

    # -- submission status (phase 10) --------------------------------------

    async def item_status(self, item_uuid: str) -> ItemStatus:
        self._record("item_status", item_uuid)
        if item_uuid in self.missing_items:
            return ItemStatus.gone(item_uuid)
        return self.item_statuses.get(item_uuid) or self.default_item_status.model_copy(
            update={"uuid": item_uuid}
        )

    # -- duplicate detection (phase 4c) -----------------------------------

    async def search_items(self, query: str | None = None, **kwargs: Any) -> Page:
        self._record("search_items", query, **kwargs)
        return page_of(self.search_results, size=kwargs.get("size", 20))
