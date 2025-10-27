"""The duplicate warning in a browser (DESIGN.md §12).

The route tests prove `/submit` answers with the warning fragment and that both
buttons work. Neither proves a browser puts the fragment where it belongs — and
this fragment is the one place in the flow where a swap *replaces* the polling
progress state rather than being replaced by it. Get the target wrong and the poll
still runs underneath, redirecting the reader away from the warning a moment after
it appears.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.conftest import SHIPPED_INSTANCE_KEY

pytestmark = pytest.mark.e2e


def choose_collection(page, live_app, fake_dspace) -> None:
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page.goto(f"{live_app}/select/c1?verified=1")
    page.wait_for_url(f"{live_app}/submit**")


@pytest.fixture
def already_deposited(app, pdf_bytes):
    """Put the file about to be uploaded into the checksum index."""
    from scholardrop.dedup.index import ChecksumIndex, IndexedBitstream, index_path_for

    with ChecksumIndex.create(
        index_path_for(app.state.settings.dedup_checksum_index_path, SHIPPED_INSTANCE_KEY),
        base_url=app.state.registry.instance(SHIPPED_INSTANCE_KEY).base_url,
    ) as index:
        index.upsert(
            [
                IndexedBitstream(
                    bitstream_uuid="bs-9",
                    checksum=hashlib.md5(pdf_bytes, usedforsecurity=False).hexdigest(),
                    algorithm="MD5",
                    item_uuid="item-9",
                    handle="11244/9999",
                    title="An Existing Thesis",
                    filename="already-here.pdf",
                )
            ]
        )


def upload(page, live_app, fake_dspace, pdf_file) -> None:
    choose_collection(page, live_app, fake_dspace)
    page.set_input_files("input[type=file]", str(pdf_file))
    page.click(".dropzone button[type=submit]")


def test_the_warning_lands_in_the_panel_and_the_poll_does_not_carry_it_away(
    signed_in_page, live_app, fake_dspace, pdf_file, already_deposited
) -> None:
    page = signed_in_page
    upload(page, live_app, fake_dspace, pdf_file)

    panel = page.wait_for_selector("#upload-panel .notice", timeout=15_000)
    assert "already has this exact file" in panel.inner_text()

    # The progress state would have redirected within a second of appearing.
    page.wait_for_timeout(2000)
    assert page.url.endswith("/submit"), "the warning must not be swapped away"
    assert "already has this exact file" in page.content()


def test_continuing_takes_the_reader_to_the_review_form(
    signed_in_page, live_app, fake_dspace, pdf_file, already_deposited
) -> None:
    page = signed_in_page
    upload(page, live_app, fake_dspace, pdf_file)
    page.wait_for_selector("#upload-panel .notice", timeout=15_000)

    page.click("text=Continue with this submission")

    page.wait_for_url(f"{live_app}/review/42", timeout=15_000)


def test_discarding_puts_the_dropzone_back(
    signed_in_page, live_app, fake_dspace, pdf_file, already_deposited
) -> None:
    page = signed_in_page
    upload(page, live_app, fake_dspace, pdf_file)
    page.wait_for_selector("#upload-panel .notice", timeout=15_000)

    page.click("text=Discard and choose another file")

    page.wait_for_selector("#upload-panel .dropzone", timeout=15_000)
    assert page.url.endswith("/submit")
    assert fake_dspace.deleted == ["42"]


def test_the_review_panel_renders_with_the_form(signed_in_page, live_app, fake_dspace) -> None:
    """Layer 2 is plain server-rendered HTML, but it shares a page with the form
    and must not displace it."""
    from tests.fakes import make_workspaceitem

    fake_dspace.workspaceitems["42"] = make_workspaceitem(
        "42",
        sections={
            "duplicates": {
                "potentialDuplicates": [{"uuid": "dup-1", "title": "An Existing Thesis"}]
            }
        },
    )
    page = signed_in_page
    page.goto(f"{live_app}/review/42")

    panel = page.wait_for_selector(".duplicate-panel", timeout=15_000)
    assert "Possibly already in" in panel.inner_text()
    assert page.query_selector("form.submission-form") is not None
    assert page.query_selector("form.submission-form button[type=submit]") is not None
