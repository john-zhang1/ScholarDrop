"""Upload in a browser: the dropzone swap, the polled progress state, the redirect.

The route tests prove `/submit` answers with a progress fragment and that the poll
answers `HX-Redirect`. Neither proves a browser puts the fragment where it belongs
or follows the redirect, which is the entire user-visible behaviour of Phase 4.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def choose_collection(page, live_app, fake_dspace) -> None:
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page.goto(f"{live_app}/select/c1?verified=1")
    page.wait_for_url(f"{live_app}/submit**")


def test_uploading_swaps_in_progress_then_lands_on_the_review_form(
    signed_in_page, live_app, fake_dspace, pdf_file
) -> None:
    page = signed_in_page
    choose_collection(page, live_app, fake_dspace)

    page.set_input_files("input[type=file]", str(pdf_file))
    page.click(".dropzone button[type=submit]")

    # The panel is replaced in place -- no navigation -- and then the poll takes
    # over and moves the browser on by itself.
    page.wait_for_url(f"{live_app}/review/42", timeout=15_000)
    assert "thesis.pdf" in page.content()


def test_the_progress_state_appears_while_extraction_runs(
    signed_in_page, live_app, fake_dspace, fake_extractor, pdf_file
) -> None:
    """Held open deliberately: the fragment must land in #upload-panel."""
    import asyncio

    fake_extractor.gate = asyncio.Event()  # never set, so the job stays running
    page = signed_in_page
    choose_collection(page, live_app, fake_dspace)

    page.set_input_files("input[type=file]", str(pdf_file))
    page.click(".dropzone button[type=submit]")

    panel = page.wait_for_selector("#upload-panel .progress", timeout=15_000)
    assert "Reading thesis.pdf" in panel.inner_text()
    assert page.url.endswith("/submit"), "the swap must not navigate"


def test_the_restricted_content_checkbox_reaches_the_server(
    signed_in_page, live_app, app, fake_dspace, fake_extractor, pdf_file
) -> None:
    """Phase 3b's promise, from the browser end: the tick must survive the post."""
    app.state.registry.instances["shareok"].extraction = "cloud"
    page = signed_in_page
    choose_collection(page, live_app, fake_dspace)

    page.check("input[name=restricted]")
    page.set_input_files("input[type=file]", str(pdf_file))
    page.click(".dropzone button[type=submit]")
    page.wait_for_url(f"{live_app}/review/42", timeout=15_000)

    _path, kwargs = fake_extractor.calls[0]
    assert kwargs["force_local"] is True
