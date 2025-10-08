"""ORCID author lookup (DESIGN.md §13.2).

Two halves, and the second is the one with teeth.

**Looking up** is assistance: a debounced search of ORCID's public API while
somebody types a name, offering iDs with the institution that makes them
distinguishable. It never blocks anything -- a slow or unreachable ORCID means no
suggestions, logged and otherwise invisible, and typing a name by hand is
unaffected.

**Storing** is where a repository can be damaged. An ORCID iD is only allowed to
become an `authority` key where the repository's authority control is actually
ORCID-based. On the instance this was written against it is not: `SolrAuthorAuthority`
controls `dc.contributor.author`, and 24,374 of its 25,009 author values carry an
authority-core uuid. Writing an ORCID iD into that key would put a dangling
reference into an index the whole repository browses by -- so the mode is per
instance, the default is to store nothing, and `authority` has to be chosen
deliberately by somebody who has checked (DESIGN.md §13.2, and the Phase 9 gate).

Nothing here knows Dublin Core: which fields hold people comes from the mapping
profile, and where an iD is stored comes from `Instance.orcid_mode`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from scholardrop.config import Instance, MappingProfile
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.orcid")

# DSpace's Choices.CF_ACCEPTED: "a person chose this from a list of candidates".
# Exactly what happened, so it is the honest value -- and it is above the
# `authority.minconfidence = ambiguous` (400) that decides whether a value is
# indexed for authority-controlled browse.
CF_ACCEPTED = 600

# 0000-0001-2345-6789, with an X check digit allowed in the last position.
ORCID_RE = re.compile(r"^(\d{4}-){3}\d{3}[\dXx]$")
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


@dataclass(frozen=True)
class OrcidPerson:
    """One search result: enough to tell two people of the same name apart."""

    orcid_id: str
    given: str = ""
    family: str = ""
    credit_name: str = ""
    institutions: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """ "Family, Given" -- the order catalogues and submission forms use.

        Falls back to the credit name (what the person asked to be called) and
        then to the iD, because an entry with no name at all is still selectable
        and must not render as a blank row.
        """
        if self.family and self.given:
            return f"{self.family}, {self.given}"
        return self.family or self.credit_name or self.given or self.orcid_id

    @property
    def affiliation(self) -> str:
        return self.institutions[0] if self.institutions else ""

    @property
    def label(self) -> str:
        """One line a person can choose from, and a screen reader can read out."""
        parts = [self.name]
        if self.affiliation:
            parts.append(self.affiliation)
        # A nameless record falls back to its iD, and "0000-… — 0000-…" reads
        # like two different things.
        if self.orcid_id != self.name:
            parts.append(self.orcid_id)
        return " — ".join(parts)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OrcidPerson:
        institutions = payload.get("institution-name") or []
        return cls(
            orcid_id=str(payload.get("orcid-id") or ""),
            given=_clean(payload.get("given-names")),
            family=_clean(payload.get("family-names")),
            credit_name=_clean(payload.get("credit-name")),
            institutions=tuple(
                name for i in institutions if isinstance(i, str) and (name := i.strip())
            ),
        )


def is_orcid(value: str) -> bool:
    return bool(ORCID_RE.match((value or "").strip()))


def _clean(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def build_query(typed: str) -> str:
    """Turn what is in the name box into an ORCID search.

    A comma is the signal that the form's own convention was used, so
    "Vaswani, Ashish" searches family and given names as such -- which is far
    more precise than throwing the whole string at the general index. Anything
    else is searched as typed.
    """
    text = " ".join((typed or "").split())
    if not text:
        return ""
    if is_orcid(text):
        return f"orcid:{text}"
    if "," in text:
        family, _, given = text.partition(",")
        family, given = family.strip(), given.strip()
        if family and given:
            return f"family-name:{_escape(family)} AND given-names:{_escape(given)}"
        text = family or given
    return _escape(text)


def _escape(value: str) -> str:
    """Quote a Lucene phrase; ORCID's search is Solr underneath.

    An unescaped `-` or `:` in a name is not a syntax error there, it is a
    *different query* -- "Smith-Jones" silently becomes "Smith NOT Jones".
    """
    return '"' + _LUCENE_SPECIAL.sub(r"\\\1", value) + '"'


async def search(
    typed: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[OrcidPerson]:
    """People matching `typed`. Never raises, never blocks: [] on any problem."""
    if not settings.orcid_enabled:
        return []
    query = build_query(typed)
    if not query or len(" ".join(typed.split())) < settings.orcid_min_query_length:
        return []

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=settings.orcid_timeout_seconds)
    try:
        response = await http.get(
            f"{settings.orcid_api_url.rstrip('/')}/expanded-search/",
            params={"q": query, "rows": settings.orcid_max_results},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Assistance that is unavailable is not an error the submitter needs to
        # hear about; they are mid-name, and the box still works.
        log.info("ORCID lookup for %r failed: %s", typed, exc)
        return []
    finally:
        if own_client:
            await http.aclose()

    results = payload.get("expanded-result") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    people = [OrcidPerson.from_payload(r) for r in results if isinstance(r, dict)]
    return [p for p in people if p.orcid_id][: settings.orcid_max_results]


@dataclass(frozen=True)
class OrcidPolicy:
    """Where this instance's iDs go, and which fields offer the lookup."""

    mode: str = "display_only"
    author_fields: frozenset[str] = frozenset()
    # The metadata field of the *primary* author (the profile's `authors`), which
    # is whose iD a single-valued target field means.
    primary_field: str = ""
    # (section, metadata field) of the parallel field, in `field:` mode -- None
    # when the mode is not `field:` or the collection's form has no such field.
    target: tuple[str, str] | None = None
    target_repeatable: bool = False
    # Set when the mode asks for a field this collection's form does not offer.
    # The lookup still runs; the iD simply has nowhere to be stored here, and the
    # review page says so rather than pretending it was saved.
    unavailable_target: str = ""

    @property
    def stores_authority(self) -> bool:
        return self.mode == "authority"

    @property
    def stores_field(self) -> bool:
        return self.target is not None

    @property
    def stores_anything(self) -> bool:
        return self.stores_authority or self.stores_field

    def offers_lookup(self, metadata_field: str) -> bool:
        return metadata_field in self.author_fields


def policy_for(
    instance: Instance,
    profile: MappingProfile,
    allowed: dict[tuple[str, str], Any] | None = None,
    *,
    enabled: bool = True,
) -> OrcidPolicy:
    """Read the instance's mode against this collection's actual form.

    `allowed` is the live field index (`patch.field_index`). A `field:` target
    that the collection does not configure cannot be patched -- DSpace only
    accepts operations on fields the submission form declares -- so it is
    reported as unavailable rather than sent and silently dropped.
    """
    author_fields = profile.author_fields()
    if not enabled or not author_fields:
        return OrcidPolicy(author_fields=frozenset())

    primary = profile.metadata_field("authors") or ""
    mode = instance.orcid_mode
    if mode == "authority":
        return OrcidPolicy(mode=mode, author_fields=author_fields, primary_field=primary)

    target_field = instance.orcid_target_field
    if not target_field:
        return OrcidPolicy(mode="display_only", author_fields=author_fields, primary_field=primary)

    for (section_id, field), config in (allowed or {}).items():
        if field == target_field:
            return OrcidPolicy(
                mode="field",
                author_fields=author_fields,
                primary_field=primary,
                target=(section_id, field),
                target_repeatable=bool(getattr(config, "repeatable", False)),
            )
    log.info("orcid_mode names %s, which this collection's form does not have", target_field)
    return OrcidPolicy(
        mode="field",
        author_fields=author_fields,
        primary_field=primary,
        unavailable_target=target_field,
    )
