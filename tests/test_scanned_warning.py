"""The scanned-PDF warning (DESIGN.md §13.4).

The reader has always known this (`PdfDocument.probably_scanned`); the fact just
never reached the person submitting. Nothing here refuses a submission: a
digitised thesis is a real deposit, and the warning exists so its submitter knows
the item will not be full-text searchable.
"""

from __future__ import annotations

import pytest

from scholardrop.extract.models import ExtractedMetadata, SourceTier, text
from scholardrop.extract.reader import PdfDocument
from scholardrop.submission.jobs import ExtractionJob, ExtractionResult

HTMX = {"HX-Request": "true"}


def upload(client, content: bytes, name: str = "scan.pdf"):
    return client.post("/submit", files={"file": (name, content, "application/pdf")}, headers=HTMX)


# ---- what the reader already knew ------------------------------------------


def test_pages_without_text_are_a_scan() -> None:
    assert PdfDocument(page_count=200, front_text="  \n ").probably_scanned is True


def test_a_document_with_text_is_not() -> None:
    assert PdfDocument(page_count=200, front_text="A" * 400).probably_scanned is False


async def test_the_real_pipeline_reports_it(tmp_path) -> None:
    """The fake extractor can claim anything; this is the path that decides."""
    import fitz

    from scholardrop.config import Instance, MappingProfile
    from scholardrop.settings import load_settings
    from scholardrop.submission.jobs import run_extraction

    document = fitz.open()
    document.new_page()  # a page, and not a word on it
    path = tmp_path / "scan.pdf"
    path.write_bytes(document.tobytes())

    result = await run_extraction(
        path,
        instance=Instance(
            key="k",
            name="n",
            base_url="https://x/server",
            mapping_profile="p",
            extraction="heuristic",
        ),
        settings=load_settings(secret_key="x"),
        profile=MappingProfile(name="p", fields={"title": "dc.title"}),
    )

    assert result.scanned is True


def test_the_job_carries_it() -> None:
    job = ExtractionJob(workspaceitem_id="42")
    job.result = ExtractionResult(metadata=ExtractedMetadata(), mapped=None, scanned=True)

    assert job.scanned is True


def test_a_job_with_no_result_claims_nothing() -> None:
    assert ExtractionJob(workspaceitem_id="42").scanned is False


# ---- the progress screen ----------------------------------------------------


def test_the_poll_warns_instead_of_redirecting(with_collection, fake_extractor, pdf_bytes) -> None:
    fake_extractor.scanned = True
    upload(with_collection, pdf_bytes)

    response = with_collection.get("/submit/status/42", headers=HTMX)

    assert response.status_code == 200
    assert "has no text layer" in response.text
    # Proceeding is one click, and the draft is untouched until it is taken.
    assert 'action="/submit/42/continue?warning=scan"' in response.text
    assert "HX-Redirect" not in response.headers


def test_a_readable_pdf_still_redirects(with_collection, fake_extractor, pdf_bytes) -> None:
    upload(with_collection, pdf_bytes)

    response = with_collection.get("/submit/status/42", headers=HTMX)

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/review/42"


def test_the_warning_can_be_switched_off(app, with_collection, fake_extractor, pdf_bytes) -> None:
    app.state.settings.scanned_warning_enabled = False
    fake_extractor.scanned = True
    upload(with_collection, pdf_bytes)

    response = with_collection.get("/submit/status/42", headers=HTMX)

    assert response.status_code == 204


def test_continuing_past_it_is_logged_as_its_own_event(
    with_collection, fake_extractor, pdf_bytes, caplog
) -> None:
    fake_extractor.scanned = True
    upload(with_collection, pdf_bytes)

    with caplog.at_level("INFO", logger="scholardrop.submit"):
        response = with_collection.post("/submit/42/continue?warning=scan", headers=HTMX)

    assert response.headers["HX-Redirect"] == "/review/42"
    assert "continued past a scan warning" in caplog.text


def test_an_unknown_warning_name_is_logged_as_the_duplicate_one(
    with_collection, fake_extractor, pdf_bytes, caplog
) -> None:
    """The audit line names one of two events, never whatever was in the URL."""
    upload(with_collection, pdf_bytes)

    with caplog.at_level("INFO", logger="scholardrop.submit"):
        with_collection.post("/submit/42/continue?warning=<script>", headers=HTMX)

    assert "continued past a duplicate warning" in caplog.text


# ---- the review form --------------------------------------------------------


def test_the_review_form_says_so_too(with_collection, fake_extractor, pdf_bytes) -> None:
    """The no-JavaScript path never sees the progress screen."""
    fake_extractor.scanned = True
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "has no text layer" in body


def test_a_scan_that_still_pre_filled_the_form_says_both(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    """Embedded metadata can fill a form that no full-text index will ever see."""
    fake_extractor.scanned = True
    fake_extractor.metadata = ExtractedMetadata(
        title=text("A Digitised Thesis", 0.9, SourceTier.XMP), tier_used=SourceTier.XMP
    )
    upload(with_collection, pdf_bytes)

    body = with_collection.get("/review/42").text

    assert "has no text layer" in body
    assert "pre-filled" in body


def test_the_form_is_silent_about_a_readable_pdf(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    upload(with_collection, pdf_bytes)

    assert "has no text layer" not in with_collection.get("/review/42").text


@pytest.mark.parametrize("enabled", [True, False])
def test_nothing_about_it_blocks_the_deposit(
    app, with_collection, fake_extractor, pdf_bytes, enabled
) -> None:
    app.state.settings.scanned_warning_enabled = enabled
    fake_extractor.scanned = True
    upload(with_collection, pdf_bytes)

    response = with_collection.post(
        "/review/42/deposit",
        data={
            "traditionalpageone::dc.title": "A Digitised Thesis",
            "traditionalpageone::dc.date.issued": "1974",
            "traditionalpageone::dc.type": "Thesis",
            "license_granted": "true",
        },
    )

    assert response.status_code == 200
    assert "Submitted for review" in response.text
