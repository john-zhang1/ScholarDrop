"""Typed views over DSpace HAL responses.

Only the fields ScholarDrop actually uses are modelled; `raw` keeps the original
payload so later phases can reach for something without a model change.

Shapes verified against DSpace 9.2 (localhost:8080/server, 2026-07-27). The one
that bites: **an empty page has no `_embedded` key at all**, so every list parse
must tolerate its absence.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scholardrop.dspace.errors import ValidationIssue


def _self_link(payload: dict[str, Any]) -> str:
    return str(payload.get("_links", {}).get("self", {}).get("href", ""))


def _link(payload: dict[str, Any], rel: str) -> str | None:
    href = payload.get("_links", {}).get(rel, {}).get("href")
    return str(href) if href else None


class PageInfo(BaseModel):
    """The `page` block DSpace attaches to every collection response."""

    number: int = 0
    size: int = 20
    total_elements: int = Field(0, alias="totalElements")
    total_pages: int = Field(0, alias="totalPages")

    model_config = ConfigDict(populate_by_name=True)

    @property
    def has_next(self) -> bool:
        return self.number + 1 < self.total_pages


class Page[T](BaseModel):
    """One page of results plus its paging metadata."""

    items: list[T] = Field(default_factory=list)
    info: PageInfo = Field(default_factory=PageInfo)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def total(self) -> int:
        return self.info.total_elements

    @property
    def has_next(self) -> bool:
        return self.info.has_next

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):  # type: ignore[override]
        return iter(self.items)

    def __bool__(self) -> bool:
        # A page with no items is falsy -- reads naturally at call sites that
        # only care whether anything came back.
        return bool(self.items)


class DSpaceObject(BaseModel):
    """Common shape of a community / collection / item resource."""

    uuid: str
    name: str = ""
    handle: str | None = None
    self_link: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Any:
        return cls(
            uuid=str(payload.get("uuid") or payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            handle=payload.get("handle"),
            self_link=_self_link(payload),
            raw=payload,
        )


class Community(DSpaceObject):
    subcommunities_link: str | None = None
    collections_link: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Community:
        base = DSpaceObject.from_payload(payload)
        return cls(
            **base.model_dump(),
            subcommunities_link=_link(payload, "subcommunities"),
            collections_link=_link(payload, "collections"),
        )


class Collection(DSpaceObject):
    """A submission target."""


class Bitstream(BaseModel):
    """One file. `checksum` is what the duplicate index is built on (DESIGN.md §12).

    DSpace records the algorithm alongside the value (`{"checkSumAlgorithm":
    "MD5", "value": "..."}`), and it is kept rather than assumed: comparing an MD5
    we computed against a digest of some other algorithm would be a false match
    dressed up as a certainty.
    """

    uuid: str
    name: str = ""
    size_bytes: int = 0
    checksum: str = ""
    checksum_algorithm: str = ""
    bundle_name: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, bundle_name: str = "") -> Bitstream:
        checksum = payload.get("checkSum") or {}
        return cls(
            uuid=str(payload.get("uuid") or payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            size_bytes=int(payload.get("sizeBytes") or 0),
            checksum=str(checksum.get("value") or "") if isinstance(checksum, dict) else "",
            checksum_algorithm=(
                str(checksum.get("checkSumAlgorithm") or "") if isinstance(checksum, dict) else ""
            ),
            bundle_name=str(payload.get("bundleName") or bundle_name or ""),
        )


class Item(DSpaceObject):
    """An archived item, as Discovery hands it back.

    `bundles` is populated only when the request asked for `embed=bundles/bitstreams`;
    `bundles_complete` says whether what arrived is all of it -- an embedded
    bitstream list is itself a page, so a bundle with more files than the embed's
    page size arrives truncated and has to be re-read (DESIGN.md §12).
    """

    metadata: dict[str, Any] = Field(default_factory=dict, repr=False)
    last_modified: str | None = None
    bundles_link: str | None = None
    bitstreams: list[Bitstream] = Field(default_factory=list)
    bundles_embedded: bool = False
    bundles_complete: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Item:
        base = DSpaceObject.from_payload(payload)
        bitstreams, embedded, complete = _embedded_bitstreams(payload)
        return cls(
            **base.model_dump(),
            metadata=payload.get("metadata") or {},
            last_modified=payload.get("lastModified"),
            bundles_link=_link(payload, "bundles"),
            bitstreams=bitstreams,
            bundles_embedded=embedded,
            bundles_complete=complete,
        )

    def first(self, metadata_field: str) -> str:
        """First value of a metadata field, or "" -- metadata is
        `{"dc.title": [{"value": ...}]}`.

        *First*, not *only*: a repeated field's `place 0` is not necessarily the
        one you want. Real data on SHAREOK has 35 items whose `dc.date.accessioned`
        reads `["today", "2019-07-02T19:20:13Z"]`. Use `values()` where every
        value matters.
        """
        return next(iter(self.values(metadata_field)), "")

    def values(self, metadata_field: str) -> list[str]:
        """Every non-empty value of a metadata field, in `place` order."""
        entries = self.metadata.get(metadata_field) or []
        if not isinstance(entries, list):
            return []
        return [
            str(entry["value"])
            for entry in entries
            if isinstance(entry, dict) and entry.get("value")
        ]

    def in_bundle(self, bundle_name: str) -> list[Bitstream]:
        return [b for b in self.bitstreams if b.bundle_name == bundle_name]


def _embedded_bitstreams(payload: dict[str, Any]) -> tuple[list[Bitstream], bool, bool]:
    """Bitstreams from `embed=bundles/bitstreams`, and whether they are all of them.

    The shape is three levels of HAL: `_embedded.bundles._embedded.bundles[]`,
    each with `_embedded.bitstreams._embedded.bitstreams[]` plus its own `page`
    block. Returns (bitstreams, was the embed there at all, is it complete).
    """
    bundles_wrapper = (payload.get("_embedded") or {}).get("bundles")
    if not isinstance(bundles_wrapper, dict):
        return [], False, True
    bundles = (bundles_wrapper.get("_embedded") or {}).get("bundles")
    if not isinstance(bundles, list):
        return [], False, True

    found: list[Bitstream] = []
    complete = True
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        name = str(bundle.get("name") or "")
        wrapper = (bundle.get("_embedded") or {}).get("bitstreams")
        if not isinstance(wrapper, dict):
            complete = False
            continue
        entries = (wrapper.get("_embedded") or {}).get("bitstreams") or []
        for entry in entries:
            if isinstance(entry, dict):
                found.append(Bitstream.from_payload(entry, bundle_name=name))
        info = PageInfo.model_validate(wrapper.get("page") or {})
        if info.has_next:
            complete = False
    return found, True, complete


class EPerson(BaseModel):
    uuid: str
    name: str = ""
    email: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EPerson:
        return cls(
            uuid=str(payload.get("uuid") or payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            email=payload.get("email"),
        )

    @property
    def display_name(self) -> str:
        return self.name or self.email or self.uuid


class AuthStatus(BaseModel):
    """`GET /api/authn/status`."""

    authenticated: bool = False
    okay: bool = True
    auth_method: str | None = None
    eperson: EPerson | None = None
    eperson_link: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AuthStatus:
        embedded = payload.get("_embedded", {}) or {}
        eperson_payload = embedded.get("eperson")
        return cls(
            authenticated=bool(payload.get("authenticated")),
            okay=bool(payload.get("okay", True)),
            auth_method=payload.get("authenticationMethod"),
            eperson=(
                EPerson.from_payload(eperson_payload) if isinstance(eperson_payload, dict) else None
            ),
            eperson_link=_link(payload, "eperson"),
        )


class WorkspaceItem(BaseModel):
    """An in-progress submission.

    `sections` is the live form data keyed by section id; `submission_definition`
    is the collection's *configuration* (which sections exist, and their fields).
    Phase 4 renders the form from the definition rather than hardcoding section
    names (DESIGN.md §3).

    **There is no item uuid here, and none can be derived.** Every `_links` entry
    on a workspaceitem is a *sub-resource* under itself -- `_links.item.href` is
    `/submission/workspaceitems/{id}/item`, not `/core/items/{uuid}` (checked
    against 9.2, 2026-07-30). Reading the uuid off the link looks like it works
    and yields the literal string `item`. The uuid only exists once the link is
    followed, which is why the deposit path resolves it *before* depositing and
    the status page merges on workspaceitem ids instead (DESIGN.md §13.3).
    """

    id: str
    self_link: str = ""
    last_modified: str | None = None
    sections: dict[str, Any] = Field(default_factory=dict)
    errors: list[ValidationIssue] = Field(default_factory=list)
    submission_definition: dict[str, Any] | None = Field(default=None, repr=False)
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WorkspaceItem:
        embedded = payload.get("_embedded", {}) or {}
        definition = embedded.get("submissionDefinition")
        raw_errors = payload.get("errors") or []
        return cls(
            id=str(payload.get("id", "")),
            self_link=_self_link(payload),
            last_modified=payload.get("lastModified"),
            sections=payload.get("sections") or {},
            errors=[ValidationIssue.from_payload(e) for e in raw_errors if isinstance(e, dict)],
            submission_definition=definition if isinstance(definition, dict) else None,
            raw=payload,
        )

    @property
    def section_ids(self) -> list[str]:
        """Sections present in the *data*."""
        return list(self.sections)

    @property
    def defined_section_ids(self) -> list[str]:
        """Sections the collection's submission definition configures, in order.

        Empty unless the response embedded the definition -- ask for it with
        `embed=submissionDefinition`.
        """
        definition = self.submission_definition or {}
        panels = (definition.get("_embedded") or {}).get("sections", {})
        if isinstance(panels, dict):
            panels = (panels.get("_embedded") or {}).get("sections", [])
        if not isinstance(panels, list):
            return []
        return [str(p.get("id")) for p in panels if isinstance(p, dict) and p.get("id")]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


class CollectionLicense(BaseModel):
    """`GET /api/core/collections/{uuid}/license` -- the deposit licence text.

    The workspaceitem's license section only carries `granted` and, once granted,
    a link to the licence *bitstream*. The text a user has to agree to before
    granting lives here.
    """

    text: str = ""
    custom: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CollectionLicense:
        return cls(text=str(payload.get("text") or ""), custom=bool(payload.get("custom")))


class VocabularyEntry(BaseModel):
    """One option of a controlled vocabulary backing a `dropdown` field.

    A closed field (`closed: true` in the form config) accepts only these values,
    so the review form has to offer them rather than a free-text box.
    """

    value: str
    display: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VocabularyEntry:
        value = str(payload.get("value") or "")
        return cls(value=value, display=str(payload.get("display") or value))


class ExternalSource(BaseModel):
    """One live-import provider the repository has configured (DESIGN.md §13.1).

    DSpace 9.2 ships around thirty of these (crossref, datacite, arxiv, pubmed,
    openalex...). Which are *usable* is a separate question from which are
    listed: a provider is only as available as the network the repository sits
    on, so an entry here is a candidate, not a promise.
    """

    id: str
    name: str = ""
    hierarchical: bool = False
    self_link: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ExternalSource:
        source_id = str(payload.get("id") or "")
        return cls(
            id=source_id,
            name=str(payload.get("name") or source_id),
            hierarchical=bool(payload.get("hierarchical")),
            self_link=_self_link(payload),
        )

    def entry_uri(self, entry_id: str) -> str:
        """The `entryValues` URI for a record, built rather than followed.

        DSpace's uri-list handler regex-matches this string; it never fetches it
        (ExternalSourceEntryItemUriListHandler). That distinction is what makes
        DOIs work at all -- a DOI's slash makes the same URI unfetchable through
        Tomcat, but perfectly readable inside a request body.
        """
        base = self.self_link.rstrip("/")
        return f"{base}/entryValues/{entry_id}"


class ExternalSourceEntry(BaseModel):
    """One record a provider found, already mapped to metadata by the server."""

    id: str
    display: str = ""
    value: str = ""
    external_source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    self_link: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ExternalSourceEntry:
        return cls(
            id=str(payload.get("id") or ""),
            display=str(payload.get("display") or ""),
            value=str(payload.get("value") or ""),
            external_source=str(payload.get("externalSource") or ""),
            metadata=payload.get("metadata") or {},
            self_link=_self_link(payload),
            raw=payload,
        )


class WorkflowItem(BaseModel):
    """A submission that entered the collection's review workflow."""

    id: str
    self_link: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WorkflowItem:
        return cls(id=str(payload.get("id", "")), self_link=_self_link(payload), raw=payload)


class ItemStatus(BaseModel):
    """Where a deposited item has got to (DESIGN.md §13.3).

    `/core/items/{uuid}` answers most of it in one read: `inArchive` is true once
    the item is public, and the handle is minted at the same moment. A false
    `inArchive` means the submission is still in progress somewhere -- in review,
    or back with its submitter after a rejection -- and only then is a second
    call needed to say which.

    `found` is false when the item is gone; `checked` is false when the lookup
    itself could not be completed, which is a different thing and has to stay
    different: "your submission was removed" and "we could not ask just now"
    must never render as the same sentence.
    """

    uuid: str = ""
    found: bool = False
    checked: bool = True
    in_archive: bool = False
    withdrawn: bool = False
    handle: str = ""
    in_workflow: bool = False
    workflow_id: str = ""
    #: Set when the submission is back with its submitter. A rejected workflow
    #: item becomes a *new* workspaceitem with a new id, so this is the only way
    #: to offer a working "Resume" for something that was already deposited once.
    workspaceitem_id: str = ""
    last_modified: str = ""

    @classmethod
    def from_item(cls, payload: dict[str, Any]) -> ItemStatus:
        return cls(
            uuid=str(payload.get("uuid") or ""),
            found=True,
            in_archive=bool(payload.get("inArchive")),
            withdrawn=bool(payload.get("withdrawn")),
            handle=str(payload.get("handle") or ""),
            last_modified=str(payload.get("lastModified") or ""),
        )

    @classmethod
    def gone(cls, uuid: str = "") -> ItemStatus:
        return cls(uuid=uuid, found=False, checked=True)

    @classmethod
    def unchecked(cls, uuid: str = "") -> ItemStatus:
        return cls(uuid=uuid, found=False, checked=False)


class DepositResult(BaseModel):
    """Outcome of POST /api/workflow/workflowitems.

    DSpace answers in one of two ways (WorkflowItemRestRepository.createAndReturn):
    201 + a workflowitem when the collection has a review workflow, or **204 with
    no body when the item was archived immediately**. Phase 5's confirmation
    screen turns on exactly this distinction (DESIGN.md §6 step 8).
    """

    archived: bool
    workflow_item: WorkflowItem | None = None
    raw: dict[str, Any] | None = Field(default=None, repr=False)

    @property
    def entered_workflow(self) -> bool:
        return not self.archived


def parse_search(payload: dict[str, Any], factory: Any) -> Page[Any]:
    """Build a Page from `discover/search/objects`.

    Discovery wraps its results twice: the response embeds a `searchResult`, whose
    own `_embedded.objects` are hit wrappers, and the thing a caller wants is each
    wrapper's `_embedded.indexableObject`. The `page` block lives on the inner
    searchResult, not the outer response -- reading the outer one gives an empty
    PageInfo and a walk that stops after the first page.
    """
    search_result = (payload.get("_embedded") or {}).get("searchResult")
    if not isinstance(search_result, dict):
        return Page(items=[], info=PageInfo.model_validate(payload.get("page") or {}))
    hits = (search_result.get("_embedded") or {}).get("objects") or []
    items = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        indexable = (hit.get("_embedded") or {}).get("indexableObject")
        if isinstance(indexable, dict):
            items.append(factory(indexable))
    return Page(items=items, info=PageInfo.model_validate(search_result.get("page") or {}))


def parse_page(
    payload: dict[str, Any],
    embedded_key: str,
    factory: Any,
) -> Page[Any]:
    """Build a Page from a HAL collection response.

    An empty result carries no `_embedded` key -- confirmed against DSpace 9.2 --
    so a missing key means "no items", not a malformed response.
    """
    embedded = payload.get("_embedded") or {}
    raw_items = embedded.get(embedded_key) or []
    items = [factory(entry) for entry in raw_items if isinstance(entry, dict)]
    info = PageInfo.model_validate(payload.get("page") or {})
    return Page(items=items, info=info)
