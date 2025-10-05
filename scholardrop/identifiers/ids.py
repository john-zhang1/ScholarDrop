"""What someone pasted, turned into an identifier (DESIGN.md §13.1).

People do not paste identifiers, they paste whatever their browser gave them: a
`doi.org` link, a PDF URL with `.pdf` on the end, `arXiv:2401.01234v2` copied off
a title page, a PubMed URL with a trailing slash, an id wrapped in angle brackets
by an email client. All of those name a record; none of them is the record's id.

This module is pure text -- no network, no repository, no Dublin Core -- so the
messy cases can be tested exhaustively and cheaply, which is the point: every
later step assumes it was handed something real.

Deliberate non-goals: ISBN, ISSN and handles are not identifiers of *a work we
can resolve metadata for* here, so they are rejected with a message rather than
half-supported. Handles in particular belong to the versioning flow (DESIGN.md
§11), which resolves them against the repository, not a registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote


class IdentifierKind(StrEnum):
    DOI = "doi"
    ARXIV = "arxiv"
    PMID = "pmid"


LABELS = {
    IdentifierKind.DOI: "DOI",
    IdentifierKind.ARXIV: "arXiv ID",
    IdentifierKind.PMID: "PubMed ID",
}


class UnknownIdentifier(ValueError):
    """The text is not an identifier ScholarDrop can resolve.

    Carries the text, so the form can say what it could not read back to the
    person who typed it.
    """

    def __init__(self, raw: str, message: str = "") -> None:
        self.raw = raw
        super().__init__(
            message
            or "That is not a DOI, arXiv ID or PubMed ID. Paste one of those, or a link "
            "containing one."
        )


@dataclass(frozen=True)
class Identifier:
    """A normalised identifier: what kind it is, and its bare value."""

    kind: IdentifierKind
    value: str

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    @property
    def display(self) -> str:
        """How to write it for a human: the form they would recognise."""
        if self.kind is IdentifierKind.ARXIV:
            return f"arXiv:{self.value}"
        if self.kind is IdentifierKind.PMID:
            return f"PMID {self.value}"
        return self.value

    @property
    def url(self) -> str:
        """Where a person can read the record this names."""
        if self.kind is IdentifierKind.DOI:
            return f"https://doi.org/{self.value}"
        if self.kind is IdentifierKind.ARXIV:
            return f"https://arxiv.org/abs/{self.value}"
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.value}/"

    @property
    def bare_arxiv(self) -> str:
        """The arXiv id without its version suffix."""
        return re.sub(r"v\d+$", "", self.value)


# A DOI is `10.<registrant>/<suffix>`; the suffix runs to the end of the token.
_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
# Post-2007 arXiv ids: YYMM.NNNN(N), optionally versioned.
_ARXIV_NEW = re.compile(r"^(\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
# Pre-2007: archive[.subject-class]/YYMMNNN, e.g. math.GT/0309136, hep-th/9901001.
_ARXIV_OLD = re.compile(r"^([a-z][a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$")
_PMID = re.compile(r"^\d{1,8}$")

# Characters an id collects on its way through prose, mail clients and slides.
_WRAPPERS = "<>()[]{}“”‘’\"'"
_TRAILING = ".,;:!?" + _WRAPPERS


def parse_identifier(raw: str) -> Identifier:
    """Read a DOI, arXiv id or PubMed id out of `raw`.

    Raises `UnknownIdentifier` rather than returning None: an identifier someone
    typed that we quietly ignored is the same failure as an extracted value
    quietly dropped, and this one is cheap to report -- nothing has happened yet.
    """
    text = (raw or "").strip().strip(_WRAPPERS).strip()
    if not text:
        raise UnknownIdentifier(raw, "Enter an identifier, or leave the box empty.")

    lowered = text.lower()

    # 1. A DOI anywhere in the text wins: doi.org links, `doi:` prefixes, and
    #    citation lines that end in one all reduce to the same thing. Checked
    #    first because an arXiv *DOI* (10.48550/arXiv...) is a DOI, not an arXiv
    #    id, and resolves against the registry rather than arXiv's API.
    if doi := _find_doi(text):
        return Identifier(IdentifierKind.DOI, doi)

    # 2. arXiv, by prefix or by URL.
    if arxiv := _find_arxiv(text, lowered):
        return Identifier(IdentifierKind.ARXIV, arxiv)

    # 3. PubMed, by prefix or by URL.
    if pmid := _find_pmid(text, lowered):
        return Identifier(IdentifierKind.PMID, pmid)

    # 4. Bare values, in decreasing distinctiveness. Only a lone token: a number
    #    at the end of a sentence is a number, and guessing otherwise would turn
    #    a mistyped title into a confident PubMed lookup.
    token = text.strip(_TRAILING) if not text.split()[1:] else ""
    if match := _ARXIV_NEW.match(token) or _ARXIV_OLD.match(token):
        return Identifier(IdentifierKind.ARXIV, match.group(1))
    if _PMID.match(token):
        # Nothing else in this vocabulary is bare digits, so a number on its own
        # is a PMID. Wrong guesses fail visibly at resolution, not silently.
        return Identifier(IdentifierKind.PMID, token)

    if lowered.startswith("hdl:") or "hdl.handle.net" in lowered or "/handle/" in lowered:
        raise UnknownIdentifier(
            raw,
            "That is a repository handle, not a registry identifier. Paste a DOI, "
            "arXiv ID or PubMed ID instead.",
        )
    raise UnknownIdentifier(raw)


def _find_doi(text: str) -> str | None:
    """The DOI in `text`, percent-decoded and stripped of what it collected."""
    # doi.org URLs percent-encode the suffix's slashes and brackets, which DOIs
    # legitimately contain (10.1002/(SICI)1097-0258...).
    candidates = (text, unquote(text))
    for candidate in candidates:
        match = _DOI.search(candidate)
        if not match:
            continue
        doi = match.group(1).rstrip(_TRAILING)
        # A URL may carry a query or fragment after the DOI; neither is part of it.
        doi = re.split(r"[?#]", doi)[0].rstrip(_TRAILING)
        return doi or None
    return None


def _find_arxiv(text: str, lowered: str) -> str | None:
    if lowered.startswith("arxiv:"):
        return _arxiv_id(text[len("arxiv:") :].strip())
    if "arxiv.org/" in lowered:
        # /abs/<id>, /pdf/<id>(v2)(.pdf), and the legacy /format/<id>.
        tail = re.split(r"arxiv\.org/(?:abs|pdf|format|ps)/", text, maxsplit=1, flags=re.IGNORECASE)
        if len(tail) == 2:
            candidate = re.split(r"[?#]", tail[1])[0]
            if candidate.lower().endswith(".pdf"):
                candidate = candidate[: -len(".pdf")]
            return _arxiv_id(candidate.strip("/"))
    return None


def _arxiv_id(candidate: str) -> str | None:
    candidate = candidate.strip().strip(_TRAILING)
    match = _ARXIV_NEW.match(candidate) or _ARXIV_OLD.match(candidate)
    return match.group(1) if match else None


def _find_pmid(text: str, lowered: str) -> str | None:
    if lowered.startswith("pmid"):
        digits = text[len("pmid") :].lstrip(": ").strip()
        return digits if _PMID.match(digits) else None
    for marker in ("pubmed.ncbi.nlm.nih.gov/", "ncbi.nlm.nih.gov/pubmed/"):
        if marker in lowered:
            tail = lowered.split(marker, 1)[1]
            digits = re.split(r"[?#/]", tail)[0].strip()
            return digits if _PMID.match(digits) else None
    return None
