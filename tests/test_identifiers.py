"""Reading an identifier out of whatever someone pasted (DESIGN.md §13.1).

Table-driven on purpose: the failures this module has to survive are all in the
input, and every row here is a shape a real person really does paste -- a
`doi.org` link, a PDF URL, an id copied off a title page with a full stop after
it, an email client's angle brackets.
"""

from __future__ import annotations

import pytest

from scholardrop.identifiers import IdentifierKind, UnknownIdentifier, parse_identifier

DOIS = [
    "10.1038/nature12373",
    "  10.1038/nature12373  ",
    "doi:10.1038/nature12373",
    "DOI: 10.1038/nature12373",
    "https://doi.org/10.1038/nature12373",
    "http://dx.doi.org/10.1038/nature12373",
    "https://www.doi.org/10.1038/nature12373",
    "<https://doi.org/10.1038/nature12373>",
    "https://doi.org/10.1038/nature12373.",
    "info:doi/10.1038/nature12373",
    "See also https://doi.org/10.1038/nature12373 for the data.",
    "https://doi.org/10.1038/nature12373?utm_source=chrome",
    "https://doi.org/10.1038/nature12373#abstract",
]

ARXIV = [
    ("2401.01234", "2401.01234"),
    ("arXiv:2401.01234", "2401.01234"),
    ("arxiv:2401.01234v2", "2401.01234v2"),
    ("https://arxiv.org/abs/1706.03762", "1706.03762"),
    ("https://arxiv.org/abs/1706.03762v7", "1706.03762v7"),
    ("https://arxiv.org/pdf/1706.03762.pdf", "1706.03762"),
    ("https://arxiv.org/pdf/1706.03762v5.pdf", "1706.03762v5"),
    ("http://export.arxiv.org/abs/1706.03762", "1706.03762"),
    ("arXiv:math.GT/0309136", "math.GT/0309136"),
    ("hep-th/9901001", "hep-th/9901001"),
]

PMIDS = [
    ("23193287", "23193287"),
    ("PMID: 23193287", "23193287"),
    ("pmid:23193287", "23193287"),
    ("https://pubmed.ncbi.nlm.nih.gov/23193287/", "23193287"),
    ("https://www.ncbi.nlm.nih.gov/pubmed/23193287", "23193287"),
]


@pytest.mark.parametrize("raw", DOIS)
def test_every_way_a_doi_arrives(raw: str) -> None:
    identifier = parse_identifier(raw)

    assert identifier.kind is IdentifierKind.DOI
    assert identifier.value == "10.1038/nature12373"


def test_a_doi_with_encoded_characters_is_decoded() -> None:
    """doi.org percent-encodes what DOIs legitimately contain."""
    identifier = parse_identifier("https://doi.org/10.1002%2F%28SICI%291097-0258")

    assert identifier.value == "10.1002/(SICI)1097-0258"


@pytest.mark.parametrize(("raw", "expected"), ARXIV)
def test_every_way_an_arxiv_id_arrives(raw: str, expected: str) -> None:
    identifier = parse_identifier(raw)

    assert identifier.kind is IdentifierKind.ARXIV
    assert identifier.value == expected


@pytest.mark.parametrize(("raw", "expected"), PMIDS)
def test_every_way_a_pubmed_id_arrives(raw: str, expected: str) -> None:
    identifier = parse_identifier(raw)

    assert identifier.kind is IdentifierKind.PMID
    assert identifier.value == expected


def test_an_arxiv_doi_is_a_doi() -> None:
    """10.48550/arXiv... resolves at the registry, not at arXiv's API."""
    identifier = parse_identifier("https://doi.org/10.48550/arXiv.2401.01234")

    assert identifier.kind is IdentifierKind.DOI
    assert identifier.value == "10.48550/arXiv.2401.01234"


def test_the_version_suffix_is_kept_but_reachable_without() -> None:
    identifier = parse_identifier("arXiv:1706.03762v7")

    assert identifier.value == "1706.03762v7"
    assert identifier.bare_arxiv == "1706.03762"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Attention Is All You Need",
        "10.1038",  # a prefix is not a DOI
        "not an identifier at all",
        "978-0-306-40615-7",  # ISBN: real, and not something we resolve
        "The answer is 42",  # a number in a sentence is not a PMID
    ],
)
def test_what_is_not_an_identifier_is_refused_by_name(raw: str) -> None:
    with pytest.raises(UnknownIdentifier):
        parse_identifier(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "https://hdl.handle.net/11244/12345",
        "hdl:11244/12345",
        "https://shareok.org/handle/11244/12345",
    ],
)
def test_a_handle_is_refused_with_the_reason(raw: str) -> None:
    """Handles belong to the versioning flow, not to a registry lookup."""
    with pytest.raises(UnknownIdentifier, match="repository handle"):
        parse_identifier(raw)


def test_each_kind_knows_where_a_person_can_read_it() -> None:
    assert parse_identifier("10.1038/nature12373").url == "https://doi.org/10.1038/nature12373"
    assert parse_identifier("arXiv:1706.03762").url == "https://arxiv.org/abs/1706.03762"
    assert parse_identifier("PMID: 23193287").url == "https://pubmed.ncbi.nlm.nih.gov/23193287/"


def test_how_each_kind_is_written_for_a_human() -> None:
    assert parse_identifier("10.1038/nature12373").display == "10.1038/nature12373"
    assert parse_identifier("2401.01234").display == "arXiv:2401.01234"
    assert parse_identifier("23193287").display == "PMID 23193287"
