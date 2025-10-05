"""Asking the registry ourselves (DESIGN.md §13.1, fallback path).

The preferred resolver is the repository's own live-import provider (see
`sources.py`): it maps the record with the repository's configuration, which is
the configuration the repository's own imports use. This module is what runs when
the instance has no provider for the identifier, or its provider fails -- and on
a stock Tomcat that is the *usual* case for DOIs, which cannot be read back
through `entryValues` at all (the slash in every DOI is refused before Spring
sees it; see docs/IMPLEMENTATION_PLAN.md, Phase 8).

Four registries, one shape out: the neutral `ExtractedMetadata` the PDF pipeline
produces, tagged `SourceTier.REGISTRY`. Nothing here knows about Dublin Core --
the instance's mapping profile decides where a publisher or a journal goes, the
same as for an extracted value.

Two rules worth keeping:

* **A registry answering "no such record" is not an error.** It is a fact the
  submitter needs to see, so it is `RecordNotFound` and the flow continues with
  whatever the PDF gave (DESIGN.md §13.1: resolution errors degrade, never block).
* **Identifiers are not document content.** Resolving a DOI leaks nothing about
  an embargoed file, so this runs even when extraction is forced local. The
  document text never comes near these calls.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.etree import ElementTree

import httpx

from scholardrop.extract.models import (
    REGISTRY_CONFIDENCE,
    Extracted,
    ExtractedMetadata,
    SourceTier,
    items,
    text,
)
from scholardrop.identifiers.ids import Identifier, IdentifierKind
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.registry")

CROSSREF_API = "https://api.crossref.org/works"
DATACITE_API = "https://api.datacite.org/dois"
DOI_RA_API = "https://doi.org/ra"
ARXIV_API = "http://export.arxiv.org/api/query"
PUBMED_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

_ATOM = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class RegistryUnavailable(Exception):
    """The registry could not be asked. The submission carries on without it."""


class RecordNotFound(RegistryUnavailable):
    """The registry was asked and has no such record. Worth telling the user."""


def _registry(value: str | None) -> Extracted[str]:
    return text(value, REGISTRY_CONFIDENCE, SourceTier.REGISTRY)


def _registry_items(values: list[str] | None) -> Extracted[list[str]]:
    return items(values, REGISTRY_CONFIDENCE, SourceTier.REGISTRY)


def user_agent(settings: Settings) -> str:
    """Identify ScholarDrop, and its operator where one is configured.

    Crossref's "polite pool" is faster and gets warned before it rate-limits, and
    the price is a contact address in the User-Agent. Without one configured this
    is still an honest agent string rather than httpx's default.
    """
    agent = "ScholarDrop/1.0 (+https://github.com/shareok; DSpace deposit assistant)"
    if settings.registry_mailto:
        agent = f"{agent} mailto:{settings.registry_mailto}"
    return agent


async def resolve(
    identifier: Identifier,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> ExtractedMetadata:
    """Fetch `identifier`'s record and return it as neutral metadata."""
    if not settings.registry_resolution_enabled:
        raise RegistryUnavailable("registry resolution is switched off")

    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=settings.registry_timeout_seconds,
        headers={"User-Agent": user_agent(settings)},
        follow_redirects=True,
    )
    try:
        if identifier.kind is IdentifierKind.DOI:
            return await _resolve_doi(http, identifier, settings)
        if identifier.kind is IdentifierKind.ARXIV:
            return await _resolve_arxiv(http, identifier)
        return await _resolve_pubmed(http, identifier)
    finally:
        if own_client:
            await http.aclose()


async def registration_agency(http: httpx.AsyncClient, doi: str) -> str:
    """Which agency registered this DOI: 'Crossref', 'DataCite', or ''.

    One cheap call that decides which API can answer at all -- a DataCite DOI is
    a 404 at Crossref and vice versa. An unreadable answer is '' and the caller
    tries Crossref first, which covers the large majority of DOIs.
    """
    try:
        response = await http.get(f"{DOI_RA_API}/{doi}")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("registration agency lookup failed for %s: %s", doi, exc)
        return ""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return str(payload[0].get("RA") or "")
    return ""


async def _resolve_doi(
    http: httpx.AsyncClient, identifier: Identifier, settings: Settings
) -> ExtractedMetadata:
    agency = await registration_agency(http, identifier.value)
    if agency.lower() == "datacite":
        return await _from_datacite(http, identifier)
    return await _from_crossref(http, identifier)


async def _get_json(http: httpx.AsyncClient, url: str, *, what: str) -> dict[str, Any]:
    try:
        response = await http.get(url)
    except httpx.HTTPError as exc:
        raise RegistryUnavailable(f"could not reach {what}: {exc}") from exc
    if response.status_code == 404:
        raise RecordNotFound(f"{what} has no record for this identifier")
    if response.status_code >= 400:
        raise RegistryUnavailable(f"{what} answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RegistryUnavailable(f"{what} did not answer with JSON") from exc
    if not isinstance(payload, dict):
        raise RegistryUnavailable(f"{what} answered an unexpected shape")
    return payload


async def _from_crossref(http: httpx.AsyncClient, identifier: Identifier) -> ExtractedMetadata:
    payload = await _get_json(http, f"{CROSSREF_API}/{identifier.value}", what="Crossref")
    work = payload.get("message")
    if not isinstance(work, dict):
        raise RecordNotFound("Crossref returned no work for this DOI")

    return ExtractedMetadata(
        title=_registry(_first_string(work.get("title"))),
        authors=_registry_items(_crossref_names(work.get("author"))),
        keywords=_registry_items(_string_list(work.get("subject"))),
        abstract=_registry(_strip_jats(work.get("abstract"))),
        date_issued=_registry(_crossref_date(work)),
        language=_registry(_as_text(work.get("language"))),
        doc_type=_registry(_readable_type(work.get("type"))),
        publisher=_registry(_as_text(work.get("publisher"))),
        journal=_registry(_first_string(work.get("container-title"))),
        doi=_registry(_as_text(work.get("DOI")) or identifier.value),
        tier_used=SourceTier.REGISTRY,
    )


async def _from_datacite(http: httpx.AsyncClient, identifier: Identifier) -> ExtractedMetadata:
    payload = await _get_json(http, f"{DATACITE_API}/{identifier.value}", what="DataCite")
    attributes = (payload.get("data") or {}).get("attributes")
    if not isinstance(attributes, dict):
        raise RecordNotFound("DataCite returned no record for this DOI")

    titles = [t.get("title") for t in attributes.get("titles") or [] if isinstance(t, dict)]
    creators = [_datacite_name(c) for c in attributes.get("creators") or [] if isinstance(c, dict)]
    subjects = [s.get("subject") for s in attributes.get("subjects") or [] if isinstance(s, dict)]
    descriptions = [
        d.get("description")
        for d in attributes.get("descriptions") or []
        if isinstance(d, dict) and d.get("descriptionType") in (None, "Abstract")
    ]
    resource_type = (attributes.get("types") or {}).get("resourceTypeGeneral")

    return ExtractedMetadata(
        title=_registry(_first_string(titles)),
        authors=_registry_items(_string_list(creators)),
        keywords=_registry_items(_string_list(subjects)),
        abstract=_registry(_first_string(descriptions)),
        date_issued=_registry(_as_text(attributes.get("publicationYear"))),
        language=_registry(_as_text(attributes.get("language"))),
        doc_type=_registry(_readable_type(resource_type)),
        publisher=_registry(_datacite_publisher(attributes.get("publisher"))),
        doi=_registry(_as_text(attributes.get("doi")) or identifier.value),
        tier_used=SourceTier.REGISTRY,
    )


async def _resolve_arxiv(http: httpx.AsyncClient, identifier: Identifier) -> ExtractedMetadata:
    try:
        response = await http.get(ARXIV_API, params={"id_list": identifier.value, "max_results": 1})
    except httpx.HTTPError as exc:
        raise RegistryUnavailable(f"could not reach arXiv: {exc}") from exc
    if response.status_code >= 400:
        # arXiv answers 400 for a malformed id and an Atom feed with no entries
        # for one that is well formed but unknown.
        raise RecordNotFound(f"arXiv answered {response.status_code} for this id")
    try:
        feed = ElementTree.fromstring(response.text)
    except ElementTree.ParseError as exc:
        raise RegistryUnavailable("arXiv did not answer with an Atom feed") from exc

    entry = feed.find("atom:entry", _ATOM)
    if entry is None:
        raise RecordNotFound("arXiv has no record for this id")
    # A deleted or unknown id still comes back as one entry, titled "Error".
    title = _node_text(entry.find("atom:title", _ATOM))
    if title.strip().lower() == "error":
        raise RecordNotFound("arXiv has no record for this id")

    authors = [
        _node_text(node.find("atom:name", _ATOM)) for node in entry.findall("atom:author", _ATOM)
    ]
    categories = [
        node.get("term", "") for node in entry.findall("atom:category", _ATOM) if node.get("term")
    ]
    published = _node_text(entry.find("atom:published", _ATOM))
    doi = _node_text(entry.find("arxiv:doi", _ATOM))
    journal = _node_text(entry.find("arxiv:journal_ref", _ATOM))

    return ExtractedMetadata(
        title=_registry(_collapse(title)),
        authors=_registry_items(_string_list(authors)),
        keywords=_registry_items(categories),
        abstract=_registry(_collapse(_node_text(entry.find("atom:summary", _ATOM)))),
        date_issued=_registry(published[:10] or None),
        doc_type=_registry("Preprint"),
        journal=_registry(journal or None),
        doi=_registry(doi or None),
        tier_used=SourceTier.REGISTRY,
    )


async def _resolve_pubmed(http: httpx.AsyncClient, identifier: Identifier) -> ExtractedMetadata:
    # esummary carries everything except the abstract; efetch would carry the
    # abstract but only as XML with a different shape per article type. The
    # abstract is the one field a PDF reliably has, so this asks for the cheap
    # document and lets extraction fill that gap.
    payload = await _get_json(
        http,
        f"{PUBMED_API}?db=pubmed&retmode=json&id={identifier.value}",
        what="PubMed",
    )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RegistryUnavailable("PubMed answered an unexpected shape")
    record = result.get(identifier.value)
    if not isinstance(record, dict) or record.get("error"):
        raise RecordNotFound("PubMed has no record for this id")

    authors = [
        str(a.get("name"))
        for a in record.get("authors") or []
        if isinstance(a, dict) and a.get("name")
    ]
    doi = next(
        (
            str(a.get("value"))
            for a in record.get("articleids") or []
            if isinstance(a, dict) and a.get("idtype") == "doi" and a.get("value")
        ),
        None,
    )
    return ExtractedMetadata(
        title=_registry(_strip_tags(record.get("title"))),
        authors=_registry_items(_string_list(authors)),
        date_issued=_registry(_pubmed_date(record)),
        language=_registry(_first_string(record.get("lang"))),
        doc_type=_registry(_readable_type(_first_string(record.get("pubtype")))),
        journal=_registry(
            _as_text(record.get("fulljournalname")) or _as_text(record.get("source"))
        ),
        doi=_registry(doi),
        tier_used=SourceTier.REGISTRY,
    )


# -- shaping ---------------------------------------------------------------


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _first_string(value: Any) -> str | None:
    if isinstance(value, list):
        return next((_as_text(v) for v in value if _as_text(v)), None)
    return _as_text(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        single = _as_text(value)
        return [single] if single else []
    return [t for v in value if (t := _as_text(v))]


def _collapse(value: str) -> str | None:
    """Atom wraps text at whatever width it likes; a title is one line."""
    return " ".join(value.split()) or None


def _node_text(node: Any) -> str:
    return (node.text or "") if node is not None else ""


def _crossref_names(authors: Any) -> list[str]:
    """Crossref's structured names, written the way a catalogue writes them.

    "Last, First" because that is what the submission forms and every existing
    record use; a `name` field (organisations, consortia) is passed through as is.
    """
    if not isinstance(authors, list):
        return []
    names = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        family = _as_text(author.get("family"))
        given = _as_text(author.get("given"))
        if family and given:
            names.append(f"{family}, {given}")
        elif family:
            names.append(family)
        elif name := _as_text(author.get("name")):
            names.append(name)
    return names


def _datacite_name(creator: dict[str, Any]) -> str | None:
    family = _as_text(creator.get("familyName"))
    given = _as_text(creator.get("givenName"))
    if family and given:
        return f"{family}, {given}"
    return _as_text(creator.get("name")) or family


def _datacite_publisher(value: Any) -> str | None:
    # DataCite 4.5 made publisher an object; earlier records are a plain string.
    if isinstance(value, dict):
        return _as_text(value.get("name"))
    return _as_text(value)


def _crossref_date(work: dict[str, Any]) -> str | None:
    """The date of record, as YYYY, YYYY-MM or YYYY-MM-DD.

    `issued` is the publication date proper; `created` is when the DOI was
    registered, which is the best available answer for a record that has not been
    formally issued.
    """
    for key in ("issued", "published", "published-print", "published-online", "created"):
        parts = (work.get(key) or {}).get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            numbers = [str(p).zfill(2) for p in parts[0] if isinstance(p, int)]
            if numbers:
                numbers[0] = numbers[0].zfill(4)
                return "-".join(numbers[:3])
    return None


def _pubmed_date(record: dict[str, Any]) -> str | None:
    """PubMed writes '2012 Nov 27' or '2012 Nov' or '2012'."""
    raw = _as_text(record.get("sortpubdate")) or _as_text(record.get("pubdate"))
    if not raw:
        return None
    # sortpubdate is '2012/11/27 00:00'; slashes are the giveaway.
    date = raw.split(" ")[0] if "/" in raw else raw
    return date.replace("/", "-")


def _readable_type(value: Any) -> str | None:
    """'journal-article' -> 'Journal article'. Registries hyphenate; forms do not."""
    kind = _as_text(value)
    if not kind:
        return None
    return kind.replace("-", " ").replace("_", " ").capitalize()


def _strip_jats(value: Any) -> str | None:
    """Crossref abstracts arrive as JATS XML. Keep the prose, drop the markup."""
    raw = _as_text(value)
    if not raw:
        return None
    stripped = _strip_tags(raw)
    if not stripped:
        return None
    # Publishers routinely prefix the tagged abstract with its own heading.
    for lead in ("Abstract", "ABSTRACT", "Summary"):
        if stripped.startswith(lead):
            stripped = stripped[len(lead) :].lstrip(" :.—-")
    return stripped or None


# Block-level markup separates words; inline markup sits inside one. Replacing
# every tag with a space turned PubMed's "GenBank<sub>.</sub>" into "GenBank ."
# on every title that uses a subscript or an italic -- which is most of biology.
_BLOCK_TAGS = re.compile(r"</?(p|div|br|sec|li|ul|ol|title|abstract)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")


def _strip_tags(value: Any) -> str | None:
    raw = _as_text(value)
    if raw is None:
        return None
    return _collapse(_ANY_TAG.sub("", _BLOCK_TAGS.sub(" ", raw))) or None
