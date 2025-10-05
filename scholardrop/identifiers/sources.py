"""Letting the repository resolve the identifier (DESIGN.md §13.1, preferred path).

DSpace ships live-import providers -- crossref, datacite, arxiv, pubmed and about
thirty more -- and a submission can be *created from* one of their records:
`POST /submission/workspaceitems` with the entry's URI as a `text/uri-list` body.
The server fetches the record and maps it with the repository's own configuration,
so an item deposited through ScholarDrop and one imported by a repository manager
from the same DOI land in the same metadata fields. That is worth more than any
mapping we could maintain here, which is why this path is tried first.

Two facts about it, both verified against 9.2 on this VM (2026-07-30):

* **The URI is parsed, not fetched.** `ExternalSourceEntryItemUriListHandler`
  regex-matches `.../externalsources/{source}/entryValues/{id}` out of the request
  *body*. So a DOI works here despite being unreadable through `entryValues` --
  Tomcat rejects an encoded slash in a path with "Invalid URI: noSlash", and an
  unencoded one matches no route.
* **A provider that has the wrong kind of identifier just says no.** Asking the
  crossref provider for a DataCite DOI is a 404 that creates nothing, so trying
  providers in order costs a round trip and never leaves a stray draft.

Everything here degrades: no provider, a provider that cannot find the record, a
provider whose upstream is down -- all of them fall through to `resolvers.py` and,
failing that, to whatever the PDF gives. An identifier never blocks a submission.
"""

from __future__ import annotations

import logging
from typing import Any

from scholardrop.dspace import DSpaceError, DSpaceForbidden, DSpaceNotFound, ExternalSource
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.identifiers.ids import Identifier, IdentifierKind

log = logging.getLogger("scholardrop.registry")

# Which of DSpace's providers can answer for which kind of identifier, in the
# order they are worth asking. DOIs are mostly Crossref's; `doi` is DSpace's
# agency-agnostic provider and a reasonable last try. Provider *ids* rather than
# a discovery rule, because the ids are DSpace's own vocabulary and stable.
PROVIDERS: dict[IdentifierKind, tuple[str, ...]] = {
    IdentifierKind.DOI: ("crossref", "datacite", "doi"),
    IdentifierKind.ARXIV: ("arxiv",),
    IdentifierKind.PMID: ("pubmed", "pubmedeu"),
}


class NoNativeSource(Exception):
    """This repository has no provider that could answer for this identifier."""


class NativeLookupFailed(Exception):
    """Every provider that could have answered, did not."""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        # `not_found` distinguishes "the registries do not have this record" --
        # worth telling the submitter -- from "the repository could not ask".
        self.not_found = not_found
        super().__init__(message)


async def available_sources(client: Any, *, cache: Any, instance_key: str) -> list[ExternalSource]:
    """The instance's providers, read once and cached like any other config.

    An instance whose DSpace cannot be asked returns an empty list rather than
    failing: the identifier then takes the fallback path.
    """
    key = (instance_key, "external-sources")
    cached = cache.external_sources.get(key)
    if cached is not None:
        return cached
    try:
        sources = await client.list_external_sources()
    except DSpaceError as exc:
        log.info("could not list external sources for %s: %s", instance_key, exc)
        sources = []
    cache.external_sources[key] = sources
    return sources


def candidates(sources: list[ExternalSource], identifier: Identifier) -> list[ExternalSource]:
    """The configured providers worth asking for this identifier, best first."""
    by_id = {source.id: source for source in sources}
    return [by_id[name] for name in PROVIDERS.get(identifier.kind, ()) if name in by_id]


async def create_from_identifier(
    client: Any,
    collection_uuid: str,
    identifier: Identifier,
    sources: list[ExternalSource],
) -> tuple[WorkspaceItem, ExternalSource]:
    """Create a draft from the repository's own resolution of `identifier`.

    Raises `NoNativeSource` when no provider could answer and `NativeLookupFailed`
    when they all declined -- neither is an error the submitter sees as a failure;
    both send the flow to `resolvers.py`.

    A `DSpaceForbidden` is *not* caught: that is the ACL wall (DESIGN.md §4) and
    it belongs to the caller's access-denied path, not to identifier handling.
    """
    usable = candidates(sources, identifier)
    if not usable:
        raise NoNativeSource(f"no provider configured for {identifier.label}")

    last_error: Exception | None = None
    missing = False
    for source in usable:
        try:
            item = await client.create_workspaceitem_from_entry(
                collection_uuid, source.entry_uri(identifier.value)
            )
        except DSpaceForbidden:
            raise
        except DSpaceNotFound as exc:
            # This provider does not have the record. The next one might.
            log.info("%s has no record for %s", source.id, identifier.display)
            last_error, missing = exc, True
            continue
        except DSpaceError as exc:
            # The provider is configured but its upstream is unreachable, slow, or
            # broken. Same outcome for the submitter; different sentence in the log.
            log.info("%s could not resolve %s: %s", source.id, identifier.display, exc)
            last_error = exc
            continue
        log.info(
            "created workspaceitem %s from %s record %s", item.id, source.id, identifier.display
        )
        return item, source

    raise NativeLookupFailed(str(last_error or "no provider answered"), not_found=missing)
