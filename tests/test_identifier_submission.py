"""Identifier-first submission, end to end through the routes (DESIGN.md §13.1).

Three things are being pinned here, and they are the three that would hurt if
they drifted:

* **either input will do** -- a PDF, an identifier, or both, but not neither;
* **the repository resolves it if it can**, and every way that can fail ends in
  an ordinary draft rather than a dead end;
* **precedence**: the registry's values win, the PDF fills the gaps, and the form
  says which is which.
"""

from __future__ import annotations

import pytest

from scholardrop.dspace import DSpaceError, DSpaceForbidden, ExternalSource
from scholardrop.extract.mapper import map_metadata
from scholardrop.extract.models import (
    REGISTRY_CONFIDENCE,
    ExtractedMetadata,
    SourceTier,
    items,
    text,
)
from scholardrop.identifiers import parse_identifier
from scholardrop.identifiers.sources import (
    NativeLookupFailed,
    NoNativeSource,
    candidates,
    create_from_identifier,
)

HTMX = {"HX-Request": "true"}
API = "https://dspace.test/server/api"
DOI = "10.1038/nature12373"


def source(source_id: str) -> ExternalSource:
    return ExternalSource(
        id=source_id,
        name=source_id,
        self_link=f"{API}/integration/externalsources/{source_id}",
    )


@pytest.fixture
def profile(shipped_config_dir):
    from scholardrop.config import load_registry

    return load_registry(shipped_config_dir).profiles["shareok-etd"]


# ---- what the form accepts ------------------------------------------------


def test_an_identifier_alone_starts_a_draft(with_collection, fake_dspace, fake_extractor) -> None:
    """No file: DESIGN.md §13.1's "paste an identifier, drop a PDF, or both"."""
    response = with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    assert response.status_code == 200
    assert fake_dspace.called("create_workspaceitem")
    path, kwargs = fake_extractor.last_call
    assert path is None
    assert kwargs["identifier"].value == DOI


def test_an_unreadable_identifier_is_refused_before_anything_happens(
    with_collection, fake_dspace
) -> None:
    """Nothing has been created yet, so this is cheap to report and rude to ignore."""
    response = with_collection.post(
        "/submit", data={"identifier": "the one about attention"}, headers=HTMX
    )

    assert response.status_code == 400
    assert "not a DOI, arXiv ID or PubMed ID" in response.text
    assert not fake_dspace.called("create_workspaceitem")
    assert not fake_dspace.called("create_workspaceitem_from_entry")


def test_the_typed_identifier_survives_a_rejection(with_collection) -> None:
    """Re-typing a 40-character DOI because the form forgot it is not acceptable."""
    response = with_collection.post("/submit", data={"identifier": "nonsense"}, headers=HTMX)

    assert 'value="nonsense"' in response.text


def test_a_pdf_and_an_identifier_together_are_both_used(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    response = with_collection.post(
        "/submit",
        data={"identifier": f"https://doi.org/{DOI}"},
        files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")},
        headers=HTMX,
    )

    assert response.status_code == 200
    path, kwargs = fake_extractor.last_call
    assert path is not None
    assert kwargs["identifier"].value == DOI


# ---- the repository's own providers ---------------------------------------


def test_the_repository_resolves_it_when_it_can(
    with_collection, fake_dspace, fake_extractor
) -> None:
    """Preferred path: the server maps the record with its own configuration."""
    fake_dspace.external_sources = [source("crossref"), source("arxiv")]
    fake_dspace.resolving_sources = {"crossref"}

    response = with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    assert response.status_code == 200
    args, _ = fake_dspace.call_args("create_workspaceitem_from_entry")
    assert args[1] == f"{API}/integration/externalsources/crossref/entryValues/{DOI}"
    assert not fake_dspace.called("create_workspaceitem")
    # The draft already holds the record, so asking the registry again would only
    # produce values the form is about to skip as "already filled".
    assert fake_extractor.last_call[1]["resolve_registry"] is False


def test_a_provider_without_the_record_hands_over_to_the_next(with_collection, fake_dspace) -> None:
    fake_dspace.external_sources = [source("crossref"), source("datacite")]
    fake_dspace.resolving_sources = {"datacite"}

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    tried = [
        args[1].rsplit("/externalsources/", 1)[-1].split("/", 1)[0]
        for name, args, _ in fake_dspace.calls
        if name == "create_workspaceitem_from_entry"
    ]
    assert tried == ["crossref", "datacite"]


def test_no_provider_at_all_falls_back_to_our_own_lookup(
    with_collection, fake_dspace, fake_extractor
) -> None:
    fake_dspace.external_sources = []

    response = with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    assert response.status_code == 200
    assert fake_dspace.called("create_workspaceitem")
    assert fake_extractor.last_call[1]["resolve_registry"] is True


def test_every_provider_declining_still_produces_a_draft(
    with_collection, fake_dspace, fake_extractor
) -> None:
    """A DOI nobody can resolve costs a notice, never the submission."""
    fake_dspace.external_sources = [source("crossref"), source("datacite"), source("doi")]
    fake_dspace.resolving_sources = set()

    response = with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    assert response.status_code == 200
    assert fake_dspace.called("create_workspaceitem")
    assert fake_extractor.last_call[1]["resolve_registry"] is True


def test_a_denied_collection_is_still_the_acl_wall(with_collection, fake_dspace) -> None:
    """DSpaceForbidden belongs to the access-denied path, not to identifiers."""
    fake_dspace.external_sources = [source("crossref")]
    fake_dspace.resolving_sources = {"crossref"}
    fake_dspace.raise_on["create_workspaceitem_from_entry"] = DSpaceForbidden(
        "not authorized", resource="collection c1"
    )

    response = with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    assert response.status_code == 403
    assert "access" in response.text.lower()


def test_the_provider_list_is_read_once_per_instance(with_collection, fake_dspace) -> None:
    """It is repository configuration, and changing it restarts DSpace."""
    fake_dspace.external_sources = [source("crossref")]
    fake_dspace.resolving_sources = {"crossref"}

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)
    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)

    assert len([c for c in fake_dspace.calls if c[0] == "list_external_sources"]) == 1


# ---- provider choice, without the routes ----------------------------------


def test_which_providers_are_worth_asking() -> None:
    configured = [source("crossref"), source("arxiv"), source("pubmed"), source("openalexPerson")]

    assert [s.id for s in candidates(configured, parse_identifier(DOI))] == ["crossref"]
    assert [s.id for s in candidates(configured, parse_identifier("arXiv:1706.03762"))] == ["arxiv"]
    assert [s.id for s in candidates(configured, parse_identifier("PMID: 1"))] == ["pubmed"]


async def test_no_configured_provider_is_its_own_answer(fake_dspace) -> None:
    with pytest.raises(NoNativeSource):
        await create_from_identifier(fake_dspace, "c1", parse_identifier(DOI), [])


async def test_a_provider_whose_upstream_is_down_is_not_a_missing_record(fake_dspace) -> None:
    """Different sentence in the log; same outcome for the submitter."""
    fake_dspace.raise_on["create_workspaceitem_from_entry"] = DSpaceError("gateway timeout")

    with pytest.raises(NativeLookupFailed) as raised:
        await create_from_identifier(fake_dspace, "c1", parse_identifier(DOI), [source("crossref")])

    assert raised.value.not_found is False


# ---- precedence -----------------------------------------------------------


def registry_metadata() -> ExtractedMetadata:
    tier = SourceTier.REGISTRY
    return ExtractedMetadata(
        title=text("The Published Title", REGISTRY_CONFIDENCE, tier),
        authors=items(["Doe, Jane"], REGISTRY_CONFIDENCE, tier),
        journal=text("Nature", REGISTRY_CONFIDENCE, tier),
        tier_used=tier,
    )


def pdf_metadata() -> ExtractedMetadata:
    tier = SourceTier.LLM
    return ExtractedMetadata(
        title=text("The Title Page's Idea Of The Title", 0.85, tier),
        authors=items(["Jane Doe", "Someone Else"], 0.85, tier),
        committee_members=items(["Roe, Richard"], 0.85, tier),
        abstract=text("An abstract the registry does not carry.", 0.85, tier),
        tier_used=tier,
    )


def test_the_registry_wins_and_the_pdf_fills_the_gaps() -> None:
    merged = registry_metadata().merged_with(pdf_metadata())

    assert merged.title.value == "The Published Title"
    assert merged.authors.value == ["Doe, Jane"]
    # The registry knows nothing about a thesis committee, or this abstract.
    assert merged.committee_members.value == ["Roe, Richard"]
    assert merged.abstract.value.startswith("An abstract")
    assert merged.tier_used is SourceTier.REGISTRY


def test_a_field_keeps_the_tier_that_supplied_it() -> None:
    merged = registry_metadata().merged_with(pdf_metadata())

    assert merged.title.source_tier is SourceTier.REGISTRY
    assert merged.committee_members.source_tier is SourceTier.LLM


def test_an_empty_registry_leaves_the_pdf_alone() -> None:
    """A DOI that would not resolve must not blank a form the PDF could fill."""
    merged = ExtractedMetadata().merged_with(pdf_metadata())

    assert merged.title.value == "The Title Page's Idea Of The Title"
    assert merged.tier_used is SourceTier.LLM


def test_merging_never_mixes_values_inside_one_field() -> None:
    """Half a registry author list topped up from a title page is neither record."""
    merged = registry_metadata().merged_with(pdf_metadata())

    assert "Someone Else" not in (merged.authors.value or [])


def test_the_mapper_records_which_tier_filled_each_field(profile) -> None:
    mapped = map_metadata(registry_metadata().merged_with(pdf_metadata()), profile)

    assert mapped.sources["dc.title"] is SourceTier.REGISTRY
    assert mapped.sources["dc.contributor.committeeMember"] is SourceTier.LLM


# ---- what the review page says --------------------------------------------


def test_the_review_page_labels_a_registry_value(
    with_collection, fake_dspace, fake_extractor
) -> None:
    fake_extractor.metadata = registry_metadata()

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)
    page = with_collection.get("/review/42")

    assert "from the registry record" in page.text
    assert f"Started from {DOI}" in page.text


def test_the_review_page_says_when_the_repository_resolved_it(with_collection, fake_dspace) -> None:
    fake_dspace.external_sources = [source("crossref")]
    fake_dspace.resolving_sources = {"crossref"}
    fake_dspace.workspaceitems = {}

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)
    page = with_collection.get("/review/42")

    assert "crossref" in page.text


def test_a_registry_that_could_not_be_reached_says_so_on_the_form(
    with_collection, fake_extractor
) -> None:
    fake_extractor.registry_notice = f"No record was found for {DOI}."

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)
    page = with_collection.get("/review/42")

    assert f"No record was found for {DOI}" in page.text


def test_a_draft_with_no_file_does_not_claim_a_pdf_failed(with_collection, fake_extractor) -> None:
    """There was no PDF, so "nothing could be extracted from this PDF" is a lie."""
    fake_extractor.metadata = ExtractedMetadata()

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)
    page = with_collection.get("/review/42")

    assert "Nothing could be extracted from this PDF" not in page.text


def test_registry_values_a_form_cannot_place_are_still_shown(
    with_collection, fake_dspace, fake_extractor
) -> None:
    """dc.relation.ispartof is not on the shipped traditional form."""
    fake_extractor.metadata = registry_metadata()
    fake_dspace.workspaceitems = {}

    with_collection.post("/submit", data={"identifier": DOI}, headers=HTMX)
    page = with_collection.get("/review/42")

    assert "Nature" in page.text
    assert "not placed" in page.text.lower()


def test_a_resumed_draft_simply_has_no_banner(with_collection, fake_dspace) -> None:
    """The session is where the identifier lives; the values are in the item."""
    fake_dspace.workspaceitems["7"] = fake_dspace.workspaceitems.get("7") or None
    page = with_collection.get("/review/7")

    assert page.status_code == 200
    assert "Started from" not in page.text
