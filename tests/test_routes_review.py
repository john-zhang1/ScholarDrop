"""The review form (DESIGN.md §6 step 6).

These tests care that the page is built from what DSpace said -- section headings,
field labels, input types, required flags -- and that extraction's contribution is
visible as a suggestion rather than as fact.
"""

from __future__ import annotations

import asyncio

from scholardrop.extract.models import ExtractedMetadata, SourceTier, items, text
from tests.fakes import make_workspaceitem, metadata_values, upload_file

HTMX = {"HX-Request": "true"}


def upload(client, pdf_bytes):
    return client.post(
        "/submit", files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )


# ---- the form comes from the repository ------------------------------------


def test_the_form_is_rendered_from_the_live_definition(with_collection, fake_dspace) -> None:
    response = with_collection.get("/review/42")

    assert response.status_code == 200
    # Sections and labels from the configuration, not from our code.
    assert "Describe your submission" in response.text
    assert "Title" in response.text and "Subject Keywords" in response.text
    assert 'name="traditionalpageone::dc.title"' in response.text
    assert fake_dspace.called("submission_form")


def test_the_sections_are_re_read_because_the_item_does_not_embed_them(
    with_collection, fake_dspace
) -> None:
    """A workspaceitem embeds its definition *without* the panels (verified
    against 9.2), so the definition has to be fetched separately -- and if that
    is skipped the form silently falls back to unordered, unlabelled sections."""
    body = with_collection.get("/review/42").text

    assert fake_dspace.called("submission_definition")
    legends = [line for line in body.splitlines() if "<legend>" in line]
    assert "Describe your submission" in legends[0], "sections must be in configured order"
    assert "Traditionalpageone" not in body, "that heading means the fallback path ran"


def test_input_types_follow_the_configuration(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert "<textarea" in body  # Abstract
    assert "<select" in body and "Thesis" in body  # dc.type from common_types
    assert 'placeholder="YYYY-MM-DD"' in body  # date input


def test_repeatable_fields_can_gain_a_row(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert "Add another" in body
    assert "/review/42/row?section=traditionalpageone&amp;field=dc.contributor.author" in body


def test_the_row_endpoint_returns_one_empty_input(with_collection) -> None:
    response = with_collection.get(
        "/review/42/row?section=traditionalpageone&field=dc.contributor.author"
    )

    assert response.status_code == 200
    assert response.text.count('name="traditionalpageone::dc.contributor.author"') == 1
    assert "Remove" in response.text


def test_a_non_repeatable_field_gets_no_extra_row(with_collection) -> None:
    response = with_collection.get("/review/42/row?section=traditionalpageone&field=dc.title")

    assert response.status_code == 200
    assert response.text.strip() == ""


def test_required_fields_are_marked_and_summarised(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert "required" in body
    assert "still to fill in" in body
    assert "This field is required." in body  # from the workspaceitem's errors array


def test_stored_values_are_shown(with_collection, fake_dspace) -> None:
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42", sections={"traditionalpageone": {"dc.title": metadata_values("A Stored Title")}}
    )

    assert "A Stored Title" in with_collection.get("/review/42").text


def test_the_attached_file_is_listed(with_collection, fake_dspace) -> None:
    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42", files=[upload_file("thesis.pdf", 2 * 1024 * 1024)]
    )

    body = with_collection.get("/review/42").text
    assert "thesis.pdf" in body
    assert "2.0 MB" in body


# ---- what extraction contributes -------------------------------------------


def test_extracted_values_prefill_the_form_and_are_labelled(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    fake_extractor.metadata = ExtractedMetadata(
        title=text("Family at the Heart", 0.85, SourceTier.LLM),
        authors=items(["Chew, Kari"], 0.85, SourceTier.LLM),
        tier_used=SourceTier.LLM,
    )
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "Family at the Heart" in body
    assert "Chew, Kari" in body
    assert "pre-filled" in body
    assert "from the PDF" in body


def test_low_confidence_values_are_flagged_for_a_human(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    fake_extractor.metadata = ExtractedMetadata(
        title=text("A guessed title", 0.3, SourceTier.HEURISTIC), tier_used=SourceTier.HEURISTIC
    )
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "A guessed title" in body
    assert "check this" in body


def test_values_with_nowhere_to_go_are_shown_not_dropped(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    fake_extractor.metadata = ExtractedMetadata(
        committee_members=items(["Dr. A, Chair"], 0.85, SourceTier.LLM),
        degree=text("Ph.D.", 0.85, SourceTier.LLM),
        tier_used=SourceTier.LLM,
    )
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "not placed" in body
    assert "Dr. A, Chair" in body
    assert "Ph.D." in body


def test_a_running_extraction_says_so_and_still_renders_the_form(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    fake_extractor.gate = asyncio.Event()
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "Still reading the PDF" in body
    assert 'name="traditionalpageone::dc.title"' in body


def test_a_failed_extraction_says_so_and_still_renders_the_form(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    fake_extractor.error = RuntimeError("the model exploded")
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "Extraction failed" in body
    assert 'name="traditionalpageone::dc.title"' in body


def test_an_empty_extraction_is_admitted(with_collection, fake_extractor, pdf_bytes) -> None:
    fake_extractor.metadata = ExtractedMetadata(
        tier_used=SourceTier.NONE, fallback_reason="no API key"
    )
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "Nothing could be extracted" in body
    assert "no API key" in body


# ---- nothing deposits in this phase ----------------------------------------


def test_the_review_page_does_not_deposit(with_collection, fake_dspace) -> None:
    with_collection.get("/review/42")

    assert not fake_dspace.called("patch_sections")
    assert not fake_dspace.called("deposit")


def test_signing_out_ends_the_form(web) -> None:
    assert web.get("/review/42").status_code == 303


def test_a_discarded_draft_is_a_page_not_a_crash(with_collection, fake_dspace) -> None:
    """Its URL stays in the browser's history after a discard."""
    from scholardrop.dspace import DSpaceNotFound

    fake_dspace.raise_on["get_workspaceitem"] = DSpaceNotFound("gone", status=404)

    response = with_collection.get("/review/42")

    assert response.status_code == 404
    assert "no longer in the repository" in response.text
    assert "/drafts" in response.text
