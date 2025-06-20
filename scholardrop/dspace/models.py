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


class WorkflowItem(BaseModel):
    """A submission that entered the collection's review workflow."""

    id: str
    self_link: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WorkflowItem:
        return cls(id=str(payload.get("id", "")), self_link=_self_link(payload), raw=payload)


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
