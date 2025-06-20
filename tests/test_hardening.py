"""Phase 6: the things that only matter when this is running in public.

Cleanup, request logging, and the cross-site check — with the rule that binds
them all: nothing that could be replayed (password, JWT, CSRF token, session id)
may ever reach a log line.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from scholardrop.session import InMemorySessionStore
from scholardrop.submission.storage import (
    discard_upload_dir,
    session_upload_dir,
    sweep_uploads,
)

HTMX = {"HX-Request": "true"}


# ---- temp-file lifetime ----------------------------------------------------


def test_uploads_are_isolated_per_session(tmp_path: Path) -> None:
    sessions = InMemorySessionStore(60)
    one, two = sessions.create(), sessions.create()

    first = session_upload_dir(tmp_path, one)
    second = session_upload_dir(tmp_path, two)

    assert first != second
    assert first.parent == second.parent == tmp_path


def test_an_abandoned_upload_directory_is_swept(tmp_path: Path) -> None:
    sessions = InMemorySessionStore(60)
    directory = session_upload_dir(tmp_path, sessions.create())
    (directory / "thesis.pdf").write_bytes(b"%PDF-1.7")

    assert sweep_uploads(tmp_path, max_age_seconds=3600) == 0, "still fresh"
    assert sweep_uploads(tmp_path, max_age_seconds=0, now=time.time() + 10) == 1
    assert not directory.exists()


def test_the_sweep_leaves_a_directory_that_was_just_written(tmp_path: Path) -> None:
    """A user still reviewing must not have the file pulled out from under them."""
    sessions = InMemorySessionStore(60)
    directory = session_upload_dir(tmp_path, sessions.create())
    old = directory / "old.pdf"
    old.write_bytes(b"%PDF-1.7")
    import os

    os.utime(directory, (0, 0))
    os.utime(old, (0, 0))
    (directory / "new.pdf").write_bytes(b"%PDF-1.7")

    assert sweep_uploads(tmp_path, max_age_seconds=3600) == 0
    assert directory.exists()


def test_sweeping_an_upload_dir_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    assert sweep_uploads(tmp_path / "nope", max_age_seconds=0) == 0


def test_signing_out_removes_the_uploaded_file(tmp_path: Path) -> None:
    sessions = InMemorySessionStore(60)
    session = sessions.create()
    directory = session_upload_dir(tmp_path, session)
    (directory / "thesis.pdf").write_bytes(b"%PDF-1.7")

    discard_upload_dir(tmp_path, session)

    assert not directory.exists()
    assert "upload_token" not in session.data


def test_logout_clears_the_session_uploads(with_collection, app, pdf_bytes) -> None:
    with_collection.post(
        "/submit", files={"file": ("t.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )
    upload_dir = app.state.settings.upload_dir
    assert list(upload_dir.iterdir()), "the upload landed somewhere"

    with_collection.post("/logout")

    assert list(upload_dir.iterdir()) == []


def test_a_deposited_file_is_removed_locally(with_collection, app, pdf_bytes) -> None:
    """Once the repository has it, ScholarDrop has no reason to keep it."""
    with_collection.post(
        "/submit", files={"file": ("t.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )
    session = next(iter(app.state.sessions._sessions.values()))
    stored = Path(session.data["upload_path"])
    assert stored.exists()

    with_collection.post(
        "/review/42/deposit",
        data={
            "traditionalpageone::dc.title": ["A Thesis"],
            "traditionalpageone::dc.date.issued": ["2016"],
            "traditionalpageone::dc.type": ["Thesis"],
            "license_granted": ["true"],
        },
    )

    assert not stored.exists()
    assert "upload_path" not in session.data


# ---- request logging -------------------------------------------------------


def test_every_request_gets_an_id_and_echoes_it_back(web) -> None:
    response = web.get("/")

    assert response.headers["X-Request-ID"]


def test_a_proxy_supplied_request_id_is_kept(web) -> None:
    response = web.get("/", headers={"X-Request-ID": "edge-abc123"})

    assert response.headers["X-Request-ID"] == "edge-abc123"


@pytest.mark.parametrize("forged", ["a b", "line\nbreak", "x" * 200, "<script>"])
def test_a_forged_request_id_is_not_taken_at_face_value(web, forged) -> None:
    """It ends up in log lines; it must not be able to forge one."""
    response = web.get("/", headers={"X-Request-ID": forged})

    assert response.headers["X-Request-ID"] != forged
    assert "\n" not in response.headers["X-Request-ID"]


def test_the_access_line_names_the_user_not_the_session(signed_in, app, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="scholardrop.access"):
        signed_in.get("/browse")

    line = caplog.text
    assert "GET /browse -> 200" in line
    assert "Jane Doe" in line
    session = next(iter(app.state.sessions._sessions.values()))
    assert session.id not in line, "the session id is a credential"


def test_credentials_never_reach_the_log(web, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        web.post("/login/shareok", data={"user": "jane@example.edu", "password": "hunter2"})

    assert "hunter2" not in caplog.text
    assert "jwt-from-login" not in caplog.text
    assert "xsrf-from-login" not in caplog.text


def test_static_files_do_not_fill_the_log(web, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="scholardrop.access"):
        web.get("/static/app.css")

    access = [r.getMessage() for r in caplog.records if r.name == "scholardrop.access"]
    assert access == []


# ---- cross-site requests ---------------------------------------------------


def test_a_post_from_another_site_is_refused(signed_in) -> None:
    response = signed_in.post(
        "/logout", headers={"Origin": "https://evil.example", "Referer": "https://evil.example/x"}
    )

    assert response.status_code == 403
    assert "did not come from ScholarDrop" in response.text


def test_a_post_from_our_own_page_is_allowed(signed_in) -> None:
    response = signed_in.post("/logout", headers={"Origin": "http://testserver"})

    assert response.status_code == 303


def test_the_referer_is_used_when_there_is_no_origin(signed_in) -> None:
    response = signed_in.post("/logout", headers={"Referer": "https://evil.example/page"})

    assert response.status_code == 403


def test_a_cross_site_get_is_not_blocked(signed_in) -> None:
    """Only state-changing methods are gated; a link from elsewhere is fine."""
    response = signed_in.get("/browse", headers={"Referer": "https://scholar.google.com/"})

    assert response.status_code == 200


def test_a_request_with_no_origin_at_all_is_allowed(signed_in) -> None:
    """curl and health checks have no Origin; forgery needs a browser, and a
    browser always tells us where it came from."""
    assert signed_in.post("/logout").status_code == 303


# ---- cookie and configuration hardening ------------------------------------


def test_the_session_cookie_is_httponly_and_samesite(web) -> None:
    response = web.post("/login/shareok", data={"user": "jane@example.edu", "password": "pw"})

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("Lax", "lax")
    assert "Path=/" in cookie


def test_production_refuses_to_start_without_a_secret_key(shipped_config_dir) -> None:
    from scholardrop.settings import load_settings

    with pytest.raises(ValueError, match="SECRET_KEY"):
        load_settings(env="production", config_dir=shipped_config_dir, secret_key="")
