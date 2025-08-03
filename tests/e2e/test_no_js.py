"""With JavaScript off (docs/MANUAL-CHECKLIST.md, browser-only items).

Depositing a thesis should not require htmx to have loaded. Two claims are made
in the code and were never checked: `/submit` falls back to an ordinary form POST
and lands on the review page (`routes/submit.upload` branches on `is_htmx`), and
the collection browser — which is htmx all the way down — says so rather than
sitting there empty.

`no_js` on each test switches the browser context; `allow_console` is not needed
because with scripts disabled there is nothing to log.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.no_js]


def test_the_collection_browser_says_it_needs_javascript(
    signed_in_page, live_app, fake_dspace
) -> None:
    page = signed_in_page
    page.goto(f"{live_app}/browse")

    assert "needs JavaScript" in page.inner_text("body")
    # And not a silent empty panel: the loading placeholder must not be the last
    # word a user without scripts sees.
    assert "Ask your repository administrator" in page.inner_text("body")


def test_uploading_still_works_without_scripts(
    signed_in_page, live_app, fake_dspace, pdf_file
) -> None:
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    page = signed_in_page
    page.goto(f"{live_app}/select/c1?verified=1")
    page.wait_for_url(f"{live_app}/submit**")

    page.set_input_files("input[type=file]", str(pdf_file))
    page.locator(".dropzone button[type=submit]").click()

    # A plain multipart POST, a 303, and the review form -- no polling involved.
    page.wait_for_url(f"{live_app}/review/42", timeout=15_000)
    assert page.locator("input[name$='dc.title']").count() >= 1


def test_signing_in_and_out_needs_no_scripts(signed_in_page, live_app) -> None:
    page = signed_in_page
    page.locator("form[action='/logout'] button").click()

    page.wait_for_url(f"{live_app}/", timeout=15_000)
    assert "Sign out" not in page.inner_text("body")
