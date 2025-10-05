"""Asking the registries ourselves (DESIGN.md §13.1, fallback path).

The payload shapes are trimmed copies of what the real APIs answer, so a test
fails when our reading of a registry is wrong rather than when a hand-made
fixture disagrees with itself. Every registry here is mocked: no test in this
suite touches the network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scholardrop.extract.models import SourceTier
from scholardrop.identifiers import parse_identifier
from scholardrop.identifiers.resolvers import (
    ARXIV_API,
    CROSSREF_API,
    DATACITE_API,
    DOI_RA_API,
    PUBMED_API,
    RecordNotFound,
    RegistryUnavailable,
    resolve,
    user_agent,
)
from scholardrop.settings import load_settings

DOI = "10.1038/nature12373"

CROSSREF_WORK = {
    "status": "ok",
    "message": {
        "DOI": DOI,
        "type": "journal-article",
        "title": ["Deep learning for institutional repositories"],
        "container-title": ["Nature"],
        "publisher": "Springer Science and Business Media LLC",
        "language": "en",
        "subject": ["Multidisciplinary"],
        "abstract": "<jats:p>Abstract We show that repositories can be searched.</jats:p>",
        "issued": {"date-parts": [[2013, 7, 24]]},
        "author": [
            {"given": "Jane", "family": "Doe", "sequence": "first"},
            {"given": "Richard", "family": "Roe", "sequence": "additional"},
            {"name": "The Consortium"},
        ],
    },
}

DATACITE_RECORD = {
    "data": {
        "attributes": {
            "doi": "10.5061/dryad.abc123",
            "titles": [{"title": "Soil samples, 2019-2021"}],
            "creators": [
                {"givenName": "Jane", "familyName": "Doe"},
                {"name": "Oklahoma Biological Survey"},
            ],
            "publisher": {"name": "Dryad"},
            "publicationYear": 2021,
            "subjects": [{"subject": "soil"}, {"subject": "ecology"}],
            "descriptions": [
                {
                    "description": "Samples collected across the state.",
                    "descriptionType": "Abstract",
                }
            ],
            "types": {"resourceTypeGeneral": "Dataset"},
            "language": "en",
        }
    }
}

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T18:44:11Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>  The dominant sequence transduction models are based on
      recurrent networks. </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
    <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">10.48550/arXiv.1706.03762</arxiv:doi>
    <arxiv:journal_ref xmlns:arxiv="http://arxiv.org/schemas/atom">NeurIPS 2017</arxiv:journal_ref>
  </entry>
</feed>
"""

ARXIV_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>ArXiv Query</title></feed>
"""

PUBMED_SUMMARY = {
    "result": {
        "uids": ["23193287"],
        "23193287": {
            "uid": "23193287",
            "title": "GenBank<sub>.</sub>",
            "sortpubdate": "2013/01/01 00:00",
            "source": "Nucleic Acids Res",
            "fulljournalname": "Nucleic acids research",
            "lang": ["eng"],
            "pubtype": ["Journal Article"],
            "authors": [{"name": "Benson DA"}, {"name": "Cavanaugh M"}],
            "articleids": [
                {"idtype": "pubmed", "value": "23193287"},
                {"idtype": "doi", "value": "10.1093/nar/gks1195"},
            ],
        },
    }
}


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        env="dev",
        secret_key="test-secret",
        upload_dir=tmp_path / "uploads",
        registry_mailto="repository@example.edu",
    )


def ra(agency: str) -> httpx.Response:
    return httpx.Response(200, json=[{"DOI": DOI, "RA": agency}])


# ---- Crossref -------------------------------------------------------------


@respx.mock
async def test_a_crossref_doi_comes_back_as_neutral_metadata(settings) -> None:
    respx.get(f"{DOI_RA_API}/{DOI}").mock(return_value=ra("Crossref"))
    respx.get(f"{CROSSREF_API}/{DOI}").mock(return_value=httpx.Response(200, json=CROSSREF_WORK))

    metadata = await resolve(parse_identifier(DOI), settings)

    assert metadata.title.value == "Deep learning for institutional repositories"
    assert metadata.authors.value == ["Doe, Jane", "Roe, Richard", "The Consortium"]
    assert metadata.journal.value == "Nature"
    assert metadata.publisher.value == "Springer Science and Business Media LLC"
    assert metadata.doi.value == DOI
    assert metadata.date_issued.value == "2013-07-24"
    assert metadata.doc_type.value == "Journal article"
    assert metadata.keywords.value == ["Multidisciplinary"]


@respx.mock
async def test_registry_values_carry_the_registry_tier(settings) -> None:
    """The review form labels them, and the merge trusts them over the PDF."""
    respx.get(f"{DOI_RA_API}/{DOI}").mock(return_value=ra("Crossref"))
    respx.get(f"{CROSSREF_API}/{DOI}").mock(return_value=httpx.Response(200, json=CROSSREF_WORK))

    metadata = await resolve(parse_identifier(DOI), settings)

    assert metadata.tier_used is SourceTier.REGISTRY
    assert metadata.title.source_tier is SourceTier.REGISTRY
    assert not metadata.title.low_confidence


@respx.mock
async def test_a_jats_abstract_arrives_as_prose(settings) -> None:
    """Crossref abstracts are XML, and a form field is not."""
    respx.get(f"{DOI_RA_API}/{DOI}").mock(return_value=ra("Crossref"))
    respx.get(f"{CROSSREF_API}/{DOI}").mock(return_value=httpx.Response(200, json=CROSSREF_WORK))

    metadata = await resolve(parse_identifier(DOI), settings)

    assert metadata.abstract.value == "We show that repositories can be searched."


@respx.mock
async def test_the_polite_pool_is_asked_for_politely(settings) -> None:
    respx.get(f"{DOI_RA_API}/{DOI}").mock(return_value=ra("Crossref"))
    route = respx.get(f"{CROSSREF_API}/{DOI}").mock(
        return_value=httpx.Response(200, json=CROSSREF_WORK)
    )

    await resolve(parse_identifier(DOI), settings)

    assert "repository@example.edu" in route.calls.last.request.headers["user-agent"]
    assert user_agent(settings).startswith("ScholarDrop/")


@respx.mock
async def test_an_unknown_doi_is_a_fact_not_a_failure(settings) -> None:
    respx.get(f"{DOI_RA_API}/{DOI}").mock(return_value=ra("Crossref"))
    respx.get(f"{CROSSREF_API}/{DOI}").mock(return_value=httpx.Response(404))

    with pytest.raises(RecordNotFound):
        await resolve(parse_identifier(DOI), settings)


@respx.mock
async def test_a_registry_that_will_not_answer_is_unavailable(settings) -> None:
    respx.get(f"{DOI_RA_API}/{DOI}").mock(return_value=ra("Crossref"))
    respx.get(f"{CROSSREF_API}/{DOI}").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(RegistryUnavailable):
        await resolve(parse_identifier(DOI), settings)


# ---- DataCite -------------------------------------------------------------


@respx.mock
async def test_the_registration_agency_routes_a_datacite_doi(settings) -> None:
    doi = "10.5061/dryad.abc123"
    respx.get(f"{DOI_RA_API}/{doi}").mock(
        return_value=httpx.Response(200, json=[{"DOI": doi, "RA": "DataCite"}])
    )
    crossref = respx.get(f"{CROSSREF_API}/{doi}").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{DATACITE_API}/{doi}").mock(return_value=httpx.Response(200, json=DATACITE_RECORD))

    metadata = await resolve(parse_identifier(doi), settings)

    assert not crossref.called
    assert metadata.title.value == "Soil samples, 2019-2021"
    assert metadata.authors.value == ["Doe, Jane", "Oklahoma Biological Survey"]
    assert metadata.publisher.value == "Dryad"
    assert metadata.date_issued.value == "2021"
    assert metadata.doc_type.value == "Dataset"


@respx.mock
async def test_an_unreadable_agency_lookup_still_tries_crossref(settings) -> None:
    """The overwhelming majority of DOIs are Crossref's; a guess beats giving up."""
    respx.get(f"{DOI_RA_API}/{DOI}").mock(side_effect=httpx.ConnectError("no dns"))
    respx.get(f"{CROSSREF_API}/{DOI}").mock(return_value=httpx.Response(200, json=CROSSREF_WORK))

    metadata = await resolve(parse_identifier(DOI), settings)

    assert metadata.title.present


# ---- arXiv ----------------------------------------------------------------


@respx.mock
async def test_an_arxiv_entry_is_unwrapped_from_its_atom_feed(settings) -> None:
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_FEED))

    metadata = await resolve(parse_identifier("arXiv:1706.03762"), settings)

    # Atom wraps text at whatever width it likes; a title is one line.
    assert metadata.title.value == "Attention Is All You Need"
    assert metadata.abstract.value.startswith("The dominant sequence transduction models")
    assert metadata.authors.value == ["Ashish Vaswani", "Noam Shazeer"]
    assert metadata.keywords.value == ["cs.CL", "cs.LG"]
    assert metadata.date_issued.value == "2017-06-12"
    assert metadata.doi.value == "10.48550/arXiv.1706.03762"
    assert metadata.journal.value == "NeurIPS 2017"
    assert metadata.doc_type.value == "Preprint"


@respx.mock
async def test_an_arxiv_feed_with_no_entry_is_not_found(settings) -> None:
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_EMPTY))

    with pytest.raises(RecordNotFound):
        await resolve(parse_identifier("arXiv:2401.01234"), settings)


# ---- PubMed ---------------------------------------------------------------


@respx.mock
async def test_a_pubmed_summary_becomes_metadata(settings) -> None:
    respx.get(url__startswith=PUBMED_API).mock(
        return_value=httpx.Response(200, json=PUBMED_SUMMARY)
    )

    metadata = await resolve(parse_identifier("PMID: 23193287"), settings)

    # Inline markup sits inside a word; only block tags become spaces.
    assert metadata.title.value == "GenBank."
    assert metadata.authors.value == ["Benson DA", "Cavanaugh M"]
    assert metadata.journal.value == "Nucleic acids research"
    assert metadata.date_issued.value == "2013-01-01"
    assert metadata.doi.value == "10.1093/nar/gks1195"
    # esummary carries no abstract; that is the gap the PDF fills.
    assert not metadata.abstract.present


@respx.mock
async def test_a_pubmed_error_record_is_not_found(settings) -> None:
    respx.get(url__startswith=PUBMED_API).mock(
        return_value=httpx.Response(
            200, json={"result": {"uids": [], "99999999": {"error": "cannot get document summary"}}}
        )
    )

    with pytest.raises(RecordNotFound):
        await resolve(parse_identifier("99999999"), settings)


# ---- the switch -----------------------------------------------------------


async def test_resolution_can_be_switched_off_entirely(tmp_path) -> None:
    """An instance that must make no outbound calls at all."""
    settings = load_settings(
        env="dev",
        secret_key="test-secret",
        upload_dir=tmp_path / "uploads",
        registry_resolution_enabled=False,
    )

    with pytest.raises(RegistryUnavailable):
        await resolve(parse_identifier(DOI), settings)
