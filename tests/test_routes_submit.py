"""Uploading the PDF (DESIGN.md §6 step 5).

The invariants worth guarding: the workspaceitem is created *before* extraction
starts, a repository refusal is an access-denied screen rather than an error, a
failed upload leaves nothing behind, and extraction can never block the flow.
"""

from __future__ import annotations

import asyncio

from scholardrop.dspace import DSpaceForbidden, DSpaceUnavailable
from tests.conftest import SHIPPED_INSTANCE_KEY

HTMX = {"HX-Request": "true"}


def upload(client, content: bytes, name: str = "thesis.pdf", *, htmx: bool = True):
    return client.post(
        "/submit",
        files={"file": (name, content, "application/pdf")},
        headers=HTMX if htmx else {},
    )


# ---- the dropzone ----------------------------------------------------------


def test_submit_shows_the_chosen_collection_and_a_dropzone(with_collection) -> None:
    response = with_collection.get("/submit")

    assert response.status_code == 200
    assert "OU - Dissertations" in response.text
    assert 'type="file"' in response.text
    assert 'hx-post="/submit"' in response.text


def test_submit_without_a_collection_goes_back_to_browse(signed_in) -> None:
    response = signed_in.get("/submit")

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


# ---- a good upload ---------------------------------------------------------


def test_upload_creates_the_workspaceitem_then_attaches_the_file(
    with_collection, fake_dspace, pdf_bytes
) -> None:
    response = upload(with_collection, pdf_bytes)

    assert response.status_code == 200
    order = [
        c[0] for c in fake_dspace.calls if c[0] in {"create_workspaceitem", "upload_bitstream"}
    ]
    assert order == ["create_workspaceitem", "upload_bitstream"], (
        "the definition must exist before the form is rendered (DESIGN.md §9)"
    )
    args, _ = fake_dspace.call_args("create_workspaceitem")
    assert args[0] == "c1"


def test_upload_answers_with_a_polling_progress_state(with_collection, pdf_bytes) -> None:
    response = upload(with_collection, pdf_bytes)

    assert 'hx-get="/submit/status/42"' in response.text
    assert "thesis.pdf" in response.text


def test_upload_starts_extraction_with_the_stored_file(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    upload(with_collection, pdf_bytes)

    assert len(fake_extractor.calls) == 1
    path, kwargs = fake_extractor.calls[0]
    assert path.read_bytes() == pdf_bytes
    assert kwargs["instance"].key == SHIPPED_INSTANCE_KEY
    assert kwargs["profile"].name == "shareok-etd"


def test_the_draft_is_remembered_on_the_session(with_collection, app, pdf_bytes) -> None:
    upload(with_collection, pdf_bytes)

    session = next(iter(app.state.sessions._sessions.values()))
    assert session.workspaceitem_id == "42"
    assert session.submission("42")["filename"] == "thesis.pdf"


def test_without_htmx_the_upload_redirects_straight_to_the_form(with_collection, pdf_bytes) -> None:
    response = upload(with_collection, pdf_bytes, htmx=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/review/42"


# ---- refusals --------------------------------------------------------------


def test_a_non_pdf_is_refused_and_nothing_is_created(with_collection, fake_dspace) -> None:
    response = upload(with_collection, b"GIF89a not a pdf", name="notes.pdf")

    assert response.status_code == 400
    assert "not a PDF" in response.text
    assert not fake_dspace.called("create_workspaceitem")


def test_an_oversized_file_is_refused(with_collection, app, fake_dspace) -> None:
    app.state.settings.upload_max_bytes = 32

    response = upload(with_collection, b"%PDF-1.7" + b"x" * 500)

    assert response.status_code == 413
    assert "larger than" in response.text
    assert not fake_dspace.called("create_workspaceitem")


def test_no_file_and_no_identifier_is_a_message_not_a_crash(with_collection) -> None:
    """Either input will do, but not neither (DESIGN.md §13.1)."""
    response = with_collection.post("/submit", headers=HTMX)

    assert response.status_code == 400
    assert "Choose a PDF to deposit, paste a DOI" in response.text


def test_a_denied_collection_renders_the_access_denied_partial(
    with_collection, fake_dspace, pdf_bytes
) -> None:
    """DSpace is the wall, not the /select gate (DESIGN.md §4)."""
    fake_dspace.raise_on["create_workspaceitem"] = DSpaceForbidden(
        "not authorized", resource="collection c1"
    )

    response = upload(with_collection, pdf_bytes)

    assert response.status_code == 403
    assert "would not start a submission" in response.text
    assert "shareok@ou.edu" in response.text
    assert not fake_dspace.called("upload_bitstream")


def test_a_failed_attachment_removes_the_empty_draft(
    with_collection, fake_dspace, pdf_bytes
) -> None:
    fake_dspace.raise_on["upload_bitstream"] = DSpaceUnavailable("boom", status=500)

    response = upload(with_collection, pdf_bytes)

    assert response.status_code == 502
    assert fake_dspace.deleted == ["42"], "no orphan draft (DESIGN.md §7)"


def test_upload_without_a_collection_goes_back_to_browse(signed_in, pdf_bytes) -> None:
    response = upload(signed_in, pdf_bytes)

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


# ---- the poll --------------------------------------------------------------


def test_the_poll_keeps_waiting_while_extraction_runs(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    fake_extractor.gate = asyncio.Event()  # never set: the job stays running

    upload(with_collection, pdf_bytes)
    response = with_collection.get("/submit/status/42", headers=HTMX)

    assert response.status_code == 200
    assert 'hx-get="/submit/status/42"' in response.text


def test_the_poll_redirects_to_the_form_when_extraction_finishes(
    with_collection, pdf_bytes
) -> None:
    upload(with_collection, pdf_bytes)

    response = with_collection.get("/submit/status/42", headers=HTMX)

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/review/42"


def test_a_forgotten_job_still_sends_the_user_to_the_form(with_collection) -> None:
    """A restart loses the job, never the submission."""
    response = with_collection.get("/submit/status/999", headers=HTMX)

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/review/999"


def test_a_failing_extractor_does_not_fail_the_upload(
    with_collection, fake_extractor, app, pdf_bytes
) -> None:
    fake_extractor.error = RuntimeError("the model exploded")

    assert upload(with_collection, pdf_bytes).status_code == 200

    with_collection.get("/submit/status/42", headers=HTMX)  # lets the task finish
    session = next(iter(app.state.sessions._sessions.values()))
    job = app.state.extractions.get(session.id, "42")
    assert job.state == "failed"
    assert job.error and "Extraction failed" in job.error


# ---- restricted content (DESIGN.md §5, Phase 3b) ---------------------------


def cloud_instance(app):
    """Put the session's instance on the cloud tier, where the choice matters."""
    app.state.registry.instances[SHIPPED_INSTANCE_KEY].extraction = "cloud"


def test_the_restricted_checkbox_is_offered_on_a_cloud_instance(with_collection, app) -> None:
    cloud_instance(app)

    response = with_collection.get("/submit")

    assert 'name="restricted"' in response.text
    assert "restricted or embargoed content" in response.text


def test_no_checkbox_where_the_document_never_leaves_anyway(with_collection, app) -> None:
    """Offering the choice on a heuristic instance would be theatre."""
    app.state.registry.instances[SHIPPED_INSTANCE_KEY].extraction = "heuristic"

    response = with_collection.get("/submit")

    assert 'name="restricted"' not in response.text


def test_ticking_it_forces_local_extraction(
    with_collection, app, fake_extractor, pdf_bytes
) -> None:
    cloud_instance(app)

    response = with_collection.post(
        "/submit",
        files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")},
        data={"restricted": "true"},
        headers=HTMX,
    )

    assert response.status_code == 200
    _path, kwargs = fake_extractor.calls[0]
    assert kwargs["force_local"] is True


def test_an_ordinary_upload_does_not_force_local(
    with_collection, fake_extractor, pdf_bytes
) -> None:
    upload(with_collection, pdf_bytes)

    _path, kwargs = fake_extractor.calls[0]
    assert kwargs["force_local"] is False


def test_the_ticked_box_survives_a_rejected_upload(with_collection, app) -> None:
    """Re-render after an error must not silently untick a privacy choice."""
    cloud_instance(app)

    response = with_collection.post(
        "/submit",
        files={"file": ("notes.pdf", b"GIF89a not a pdf", "application/pdf")},
        data={"restricted": "true"},
        headers=HTMX,
    )

    assert response.status_code == 400
    assert "checked" in response.text
