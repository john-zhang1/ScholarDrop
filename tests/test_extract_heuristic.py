"""Tier 2 extraction against front-matter fixtures.

The three shipped fixtures are synthetic but shaped like real documents (see
tests/fixtures/README.md). Any additional `*_front.txt` dropped into that directory
is picked up by the sweep tests below, so real ETD text can be added without
touching this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholardrop.extract import heuristic
from scholardrop.extract.models import LOW_CONFIDENCE, SourceTier
from scholardrop.extract.reader import PdfDocument, TextBlock

FIXTURES = Path(__file__).parent / "fixtures"


def document_from(name: str, **kwargs) -> PdfDocument:
    return PdfDocument(page_count=120, front_text=(FIXTURES / name).read_text(), **kwargs)


@pytest.fixture
def dissertation() -> PdfDocument:
    return document_from("dissertation_front.txt")


@pytest.fixture
def thesis() -> PdfDocument:
    return document_from("thesis_front.txt")


@pytest.fixture
def article() -> PdfDocument:
    return document_from("article_front.txt")


# ---- dissertation: separate committee page, block ABSTRACT heading ----------


def test_dissertation_abstract_is_verbatim_and_stops_at_the_next_heading(dissertation) -> None:
    abstract = heuristic.extract(dissertation).abstract

    assert abstract.present
    assert abstract.value.startswith("Ephemeral streams across the southern Great Plains")
    assert abstract.value.endswith("throughout the region")
    assert "Keywords" not in abstract.value
    assert "Table of Contents" not in abstract.value
    assert "Chapter 1" not in abstract.value


def test_dissertation_keywords(dissertation) -> None:
    keywords = heuristic.extract(dissertation).keywords

    assert "fluvial geomorphology" in keywords.value
    assert "riparian vegetation" in keywords.value
    assert len(keywords.value) == 6


def test_dissertation_committee_separates_the_chair(dissertation) -> None:
    result = heuristic.extract(dissertation)

    assert result.advisor.value == "Helen Vasquez"
    # Asserting the *exact* list, not just membership: a loose assertion here let
    # "All Rights Reserved" (three capitalised words, straight after the committee
    # list) through as a committee member until a real CLI run showed it.
    assert result.committee_members.value == [
        "Samuel Adeyemi",
        "Priya Raghunathan",
        "Thomas Lindqvist",
        "Anne-Marie Cloutier",
    ]


def test_copyright_boilerplate_is_not_a_committee_member() -> None:
    front = (
        "Dissertation Committee:\n\n"
        "Dr. Helen Vasquez, Chair\nDr. Samuel Adeyemi\n\n"
        "© Copyright by MARGARET OKONKWO-BELL 2019\nAll Rights Reserved.\n"
    )

    result = heuristic.extract(PdfDocument(page_count=140, front_text=front))

    assert result.committee_members.value == ["Samuel Adeyemi"]


def test_dissertation_author_from_the_by_line(dissertation) -> None:
    """The commonest ETD author pattern, and not present in DocInfo."""
    authors = heuristic.extract(dissertation).authors

    assert authors.value == ["MARGARET OKONKWO-BELL"]
    assert authors.source_tier is SourceTier.HEURISTIC
    assert authors.low_confidence


@pytest.mark.parametrize(
    ("front", "expected"),
    [
        # The middle initial's period survives: it is part of the name as printed.
        ("Title\n\nby\n\nCortez, Daniel R.\n\nA thesis\n", ["Cortez, Daniel R."]),
        ("Title\n\nBy Jane Q. Doe\n\nSubmitted\n", ["Jane Q. Doe"]),
        ("Title\n\nBy\n\nJane Doe\nRichard Roe\n\n2020\n", ["Jane Doe", "Richard Roe"]),
        ("Title\n\nBy\n\nJane Doe, B.S.\n", ["Jane Doe"]),
    ],
)
def test_by_line_variants(front: str, expected: list[str]) -> None:
    assert heuristic.extract(PdfDocument(page_count=5, front_text=front)).authors.value == expected


@pytest.mark.parametrize(
    "front",
    [
        "Title\n\nBy\n\nThe University of Oklahoma\n",  # institution, not a person
        "Title\n\nBy\n\nDownloaded from sagepub.com\n",  # publisher banner
        "Title\n\nBy\n\n2019\n",  # a year
        "Title\n\nBy\n\nsubmitted in partial fulfillment\n",
    ],
)
def test_by_line_rejects_non_people(front: str) -> None:
    assert heuristic.extract(PdfDocument(page_count=5, front_text=front)).authors.present is False


def test_metadata_authors_win_over_the_by_line() -> None:
    front = "Title\n\nBy\n\nSOMEONE ELSE ENTIRELY\n"
    document = PdfDocument(page_count=5, front_text=front, docinfo={"author": "Doe, Jane"})

    authors = heuristic.extract(document).authors

    assert authors.value == ["Doe, Jane"]
    assert authors.source_tier is SourceTier.XMP


def test_dissertation_honorifics_are_stripped(dissertation) -> None:
    result = heuristic.extract(dissertation)

    for name in [result.advisor.value, *result.committee_members.value]:
        assert not name.startswith(("Dr", "Prof", "Mr", "Ms"))


def test_dissertation_degree_and_year(dissertation) -> None:
    result = heuristic.extract(dissertation)

    assert result.degree.value == "Doctor of Philosophy"
    assert result.date_issued.value == "2019"


# ---- thesis: run-in ABSTRACT heading, "Committee Chair" wording -------------


def test_thesis_run_in_abstract_heading(thesis) -> None:
    abstract = heuristic.extract(thesis).abstract

    assert abstract.present
    assert abstract.value.startswith("Between 1930 and 1945")
    assert "Acknowledgements" not in abstract.value


def test_thesis_committee(thesis) -> None:
    result = heuristic.extract(thesis)

    assert result.advisor.value == "Ingrid Solberg"
    assert result.committee_members.value == ["Nathaniel Ozawa", "Fatima El-Sayed"]


def test_thesis_has_no_keywords_and_says_so(thesis) -> None:
    keywords = heuristic.extract(thesis).keywords

    assert keywords.present is False
    assert keywords.value == []


def test_thesis_degree_and_department(thesis) -> None:
    result = heuristic.extract(thesis)

    assert result.degree.value == "Master of Arts"
    assert result.department.present
    assert "History" in result.department.value


# ---- article: no committee at all ------------------------------------------


def test_article_has_no_committee(article) -> None:
    result = heuristic.extract(article)

    assert result.advisor.present is False
    assert result.committee_members.value == []


def test_article_abstract_and_keywords(article) -> None:
    result = heuristic.extract(article)

    assert result.abstract.value.startswith("Predicting runoff in ungauged catchments")
    assert "1. Introduction" not in result.abstract.value
    assert "ungauged catchments" in result.keywords.value
    assert "karst hydrology" in result.keywords.value


def test_article_has_no_degree(article) -> None:
    assert heuristic.extract(article).degree.present is False


# ---- provenance and confidence --------------------------------------------


def test_heuristic_values_are_flagged_for_review(dissertation) -> None:
    """Everything a heuristic infers must land below the review threshold."""
    result = heuristic.extract(dissertation)

    for name, extracted in result.present_fields().items():
        if extracted.source_tier is SourceTier.HEURISTIC:
            assert extracted.confidence < LOW_CONFIDENCE, name
            assert extracted.low_confidence, name


def test_xmp_title_outranks_layout_and_is_not_flagged() -> None:
    document = PdfDocument(
        page_count=10,
        front_text="Some body text that is long enough to be plausible.",
        docinfo={"title": "A Real Authored Title About Something"},
    )

    title = heuristic.extract(document).title

    assert title.value == "A Real Authored Title About Something"
    assert title.source_tier is SourceTier.XMP
    assert title.low_confidence is False


@pytest.mark.parametrize(
    "junk", ["Untitled", "Microsoft Word", "thesis_final_v3.docx", "Document", ""]
)
def test_junk_docinfo_titles_are_rejected(junk: str) -> None:
    document = PdfDocument(page_count=1, front_text="body", docinfo={"title": junk})

    assert heuristic.extract(document).title.present is False


def test_docinfo_authors_are_split(dissertation) -> None:
    document = document_from(
        "dissertation_front.txt", docinfo={"author": "Okonkwo-Bell, Margaret; Vasquez, Helen"}
    )

    authors = heuristic.extract(document).authors

    assert authors.value == ["Okonkwo-Bell, Margaret", "Vasquez, Helen"]
    assert authors.source_tier is SourceTier.XMP


def test_xmp_with_an_empty_title_element_does_not_leak_markup() -> None:
    """Regression: observed on a real dissertation in the assetstore.

    An empty dc:title let a `.*?` capture run on into dc:creator, producing the
    title "</rdf:Alt> </dc:title> <dc:creator> <rdf:Seq> <rdf:li>Kari Chew".
    """
    xmp = """<rdf:RDF><rdf:Description>
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default"></rdf:li></rdf:Alt></dc:title>
      <dc:creator><rdf:Seq><rdf:li>Chew, Kari</rdf:li></rdf:Seq></dc:creator>
    </rdf:Description></rdf:RDF>"""
    document = PdfDocument(page_count=200, front_text="body text", xmp=xmp)

    result = heuristic.extract(document)

    assert result.title.present is False
    assert "<" not in (result.title.value or "")
    assert result.authors.value == ["Chew, Kari"]


@pytest.mark.parametrize(
    "banner",
    [
        "from the SAGE Social Science Collections. All Rights Reserved. at UNIV OF "
        "OKLAHOMA LIBRARIES on January 20, 2016 jmd.sagepub.com Downloaded from",
        "Downloaded from https://academic.example.com/article/12345 by guest",
        "This content downloaded from 129.15.14.2 on Tue, 12 Jan 2016",
    ],
)
def test_publisher_banners_are_not_titles(banner: str) -> None:
    """Regression: banners are the largest block on many real article PDFs."""
    from scholardrop.extract.reader import TextBlock

    document = PdfDocument(
        page_count=13,
        front_text="body",
        first_page_blocks=[TextBlock(text=banner, max_size=28.0, top=20.0)],
    )

    assert heuristic.extract(document).title.present is False


def test_metadata_authors_are_flagged_for_review() -> None:
    """A PDF Author field is often the journal, e.g. "IJNC Editorial Committee"."""
    document = PdfDocument(
        page_count=25, front_text="body", docinfo={"author": "IJNC Editorial Committee"}
    )

    authors = heuristic.extract(document).authors

    assert authors.value == ["IJNC Editorial Committee"]
    assert authors.low_confidence, "the reviewer must be prompted to check this"


def test_repeated_title_page_year_beats_a_stray_one() -> None:
    """Regression: 'last year seen' picked 1994 off a real dissertation's page 1."""
    front = (
        "UNIVERSITY OF OKLAHOMA\n\nA DISSERTATION\n\nBy KARI CHEW\nNorman, Oklahoma\n2016\n\n"
        "© Copyright by KARI CHEW 2016\n\nciting an earlier work from 1994 and 1987\n"
    )

    assert heuristic.extract(PdfDocument(page_count=200, front_text=front)).date_issued.value == (
        "2016"
    )


def test_implausible_years_are_ignored() -> None:
    front = "Submitted 3024 pages of appendices, catalogue number 1066\n"

    assert heuristic.extract(PdfDocument(page_count=5, front_text=front)).date_issued.present is (
        False
    )


def test_xmp_xml_is_read_when_docinfo_is_empty() -> None:
    xmp = """<rdf:RDF><rdf:Description>
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Title From XMP Metadata</rdf:li>
      </rdf:Alt></dc:title>
      <dc:creator><rdf:Seq><rdf:li>Ferrari, Lucia</rdf:li></rdf:Seq></dc:creator>
    </rdf:Description></rdf:RDF>"""
    document = PdfDocument(page_count=3, front_text="body text here", xmp=xmp)

    result = heuristic.extract(document)

    assert result.title.value == "Title From XMP Metadata"
    assert result.authors.value == ["Ferrari, Lucia"]


# ---- title from layout ----------------------------------------------------


def test_title_from_largest_block_ignoring_boilerplate() -> None:
    from scholardrop.extract.reader import TextBlock

    document = PdfDocument(
        page_count=5,
        front_text="body",
        first_page_blocks=[
            TextBlock(text="UNIVERSITY OF OKLAHOMA", max_size=11.0, top=40.0),
            TextBlock(text="A Study of Something Interesting", max_size=20.0, top=200.0),
            TextBlock(text="A DISSERTATION submitted to the faculty", max_size=24.0, top=300.0),
            TextBlock(text="By", max_size=30.0, top=400.0),
        ],
    )

    title = heuristic.extract(document).title

    # Bigger blocks exist, but they are boilerplate ("A DISSERTATION...") or a
    # single word ("By").
    assert title.value == "A Study of Something Interesting"
    assert title.source_tier is SourceTier.HEURISTIC


def test_no_blocks_and_no_metadata_means_no_title() -> None:
    assert heuristic.extract(PdfDocument(page_count=1)).title.present is False


# The real page-1 layout of Xu_oklahoma_2409A_10865.pdf: a Word-typeset ETD title
# page, every block 12pt, the title wrapped over two lines. Before this, tier 2
# returned "GRADUATE COLLEGE" at 0.30 confidence and it was deposited.
def _uniform_etd_title_page() -> list[TextBlock]:
    lines = [
        ("UNIVERSITY OF OKLAHOMA", 108.49),
        ("GRADUATE COLLEGE", 136.09),
        ("DETECTION, DISTRIBUTIONS, AND BIOGEOCHEMICAL IMPLICATIONS OF", 242.92),
        ("FOSSIL ARSENOLIPIDS IN MARINE AND LACUSTRINE SEDIMENTS", 270.52),
        ("A DISSERTATION", 358.12),
        ("SUBMITTED TO THE GRADUATE FACULTY", 385.72),
        ("in partial fulfillment of the requirements for the", 413.34),
        ("Degree of", 440.94),
        ("DOCTOR OF PHILOSOPHY", 468.54),
        ("By", 609.81),
        ("CHENXI XU", 637.29),
        ("Norman, Oklahoma", 664.89),
        ("2026", 692.48),
    ]
    return [TextBlock(text=text, max_size=12.0, top=top) for text, top in lines]


def test_wrapped_title_on_a_uniform_font_page_is_read_whole() -> None:
    document = PdfDocument(
        page_count=151, front_text="body", first_page_blocks=_uniform_etd_title_page()
    )

    title = heuristic.extract(document).title

    assert title.value == (
        "DETECTION, DISTRIBUTIONS, AND BIOGEOCHEMICAL IMPLICATIONS OF "
        "FOSSIL ARSENOLIPIDS IN MARINE AND LACUSTRINE SEDIMENTS"
    )
    # Nothing about the layout confirmed this, so the reviewer still gets a flag.
    assert title.low_confidence is True


def test_letterhead_is_never_the_title() -> None:
    document = PdfDocument(
        page_count=151, front_text="body", first_page_blocks=_uniform_etd_title_page()
    )

    assert "GRADUATE COLLEGE" not in (heuristic.extract(document).title.value or "")


def test_uniform_page_with_rival_candidates_declines_rather_than_guessing() -> None:
    """No size hierarchy and more than one survivor is a coin toss, not a title."""
    document = PdfDocument(
        page_count=5,
        front_text="body",
        first_page_blocks=[
            TextBlock(text="Some Plausible Heading Here", max_size=12.0, top=100.0),
            TextBlock(text="Another Plausible Heading", max_size=12.0, top=300.0),
            TextBlock(text="A Third Thing Entirely", max_size=12.0, top=500.0),
        ],
    )

    assert heuristic.extract(document).title.present is False


def test_wrapping_does_not_swallow_the_next_element() -> None:
    """A real gap between elements is not leading, whatever the font size."""
    document = PdfDocument(
        page_count=5,
        front_text="body",
        first_page_blocks=[
            TextBlock(text="A Study of Something Interesting", max_size=12.0, top=200.0),
            TextBlock(text="Norman, Oklahoma", max_size=12.0, top=600.0),
        ],
    )

    # Two candidates, no hierarchy -- the point is that they stayed two.
    assert heuristic.extract(document).title.present is False


# ---- graceful degradation -------------------------------------------------


def test_empty_document_extracts_nothing_without_raising() -> None:
    result = heuristic.extract(PdfDocument(page_count=0))

    assert result.is_empty()


def test_scanned_document_extracts_nothing_without_raising() -> None:
    result = heuristic.extract(PdfDocument(page_count=200, front_text="   \n \n "))

    assert result.is_empty()


def test_garbage_text_does_not_invent_fields() -> None:
    noise = "\n".join("lorem ipsum dolor sit amet consectetur" for _ in range(40))

    result = heuristic.extract(PdfDocument(page_count=9, front_text=noise))

    assert result.abstract.present is False
    assert result.advisor.present is False
    assert result.committee_members.value == []
    assert result.keywords.value == []


# ---- sweep every fixture, including any real ones added later -------------


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURES.glob("*_front.txt"))


def test_fixtures_are_present() -> None:
    assert len(_fixture_paths()) >= 3


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_every_fixture_yields_a_title_and_abstract(path: Path) -> None:
    document = PdfDocument(page_count=100, front_text=path.read_text())

    result = heuristic.extract(document)

    assert result.abstract.present, f"{path.name}: no abstract found"
    assert len(result.abstract.value) > 100


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_no_fixture_produces_a_duplicated_committee_member(path: Path) -> None:
    result = heuristic.extract(PdfDocument(page_count=100, front_text=path.read_text()))

    members = result.committee_members.value or []
    assert len(members) == len(set(members))
    if result.advisor.present:
        assert result.advisor.value not in members
