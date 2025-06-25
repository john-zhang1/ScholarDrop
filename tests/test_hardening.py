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
    stored = Path(session.submission("42")["path"])
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
    assert session.submission("42") == {}, "the draft's scratch space goes with it"


def test_depositing_one_draft_leaves_another_tabs_file_alone(
    with_collection, app, pdf_bytes, fake_dspace
) -> None:
    """Two tabs are two drafts. Per-session slots made them share one file path,
    so depositing the first deleted the second's upload out from under it."""
    with_collection.post(
        "/submit", files={"file": ("first.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )
    session = next(iter(app.state.sessions._sessions.values()))
    first = Path(session.submission("42")["path"])

    # A second tab starts another submission; DSpace hands back a different id.
    fake_dspace.next_workspaceitem_id = "43"
    with_collection.post(
        "/submit", files={"file": ("second.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )
    second = Path(session.submission("43")["path"])
    assert first != second and first.exists() and second.exists()

    with_collection.post(
        "/review/43/deposit",
        data={
            "traditionalpageone::dc.title": ["The Second One"],
            "traditionalpageone::dc.date.issued": ["2016"],
            "traditionalpageone::dc.type": ["Thesis"],
            "license_granted": ["true"],
        },
    )

    assert not second.exists(), "the deposited draft's file is removed"
    assert first.exists(), "the other tab's file must survive"
    assert session.submission("42")["filename"] == "first.pdf"


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


# ---- the session cookie slides with the session ----------------------------


def test_an_active_session_keeps_its_cookie_alive(signed_in, app) -> None:
    """The cookie's lifetime has to follow the idle timeout, not the sign-in time.

    Otherwise the browser drops it a fixed TTL after login however active the user
    was -- and someone part-way through a long review form posts with no cookie and
    loses every edit, because the form lives in the page until deposit.
    """
    response = signed_in.get("/browse")

    cookie = response.headers.get("set-cookie")
    assert cookie is not None, "an authenticated request must re-issue the cookie"
    assert f"Max-Age={app.state.settings.session_ttl_seconds}" in cookie
    assert "HttpOnly" in cookie


def test_signing_out_is_not_undone_by_the_refresh(signed_in, app) -> None:
    """Logout deletes the cookie; the refresh must not put it back."""
    response = signed_in.post("/logout")

    cookie = response.headers["set-cookie"]
    assert 'scholardrop_session=""' in cookie or "scholardrop_session=;" in cookie
    assert "Max-Age=0" in cookie or "expires=Thu, 01 Jan 1970" in cookie.lower()
    assert len(app.state.sessions) == 0


def test_an_expired_session_is_not_refreshed(web, app) -> None:
    """A cookie whose session is gone gets nothing -- no silent resurrection."""
    web.post("/login/shareok", data={"user": "jane@example.edu", "password": "pw"})
    for session_id in list(app.state.sessions._sessions):
        app.state.sessions.delete(session_id)

    response = web.get("/")

    assert "set-cookie" not in response.headers


def test_static_requests_do_not_extend_a_session(signed_in, app) -> None:
    """The refresh reads the session without touching it, so asset traffic alone
    cannot keep an abandoned tab signed in forever."""
    session = next(iter(app.state.sessions._sessions.values()))
    session.last_seen -= 100
    before = session.last_seen

    signed_in.get("/static/app.css")

    assert session.last_seen == before


# ---- the sweep loop survives a bad iteration -------------------------------


async def test_a_failing_sweep_does_not_end_the_loop(app, monkeypatch) -> None:
    """This task is the only thing that removes uploaded theses nobody cleaned up.
    If one bad iteration killed it they would sit on disk until a restart."""
    import asyncio

    from scholardrop import app as app_module

    app.state.settings.session_sweep_interval_seconds = 0
    calls = []
    third_call = asyncio.Event()

    def exploding_sweep(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the shared mount went away")
        if len(calls) >= 3:
            third_call.set()
        return 0

    monkeypatch.setattr(app_module, "sweep_uploads", exploding_sweep)

    task = asyncio.create_task(app_module._sweep(app))
    try:
        # Bounded: if the failure ended the loop this times out and fails, rather
        # than hanging the suite waiting for a tick that will never come.
        await asyncio.wait_for(third_call.wait(), timeout=5)
    finally:
        task.cancel()

    assert len(calls) >= 3, "the loop kept running after the failure"


# ---- content security policy -----------------------------------------------


def test_pages_carry_a_content_security_policy(web) -> None:
    policy = web.get("/").headers["content-security-policy"]

    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self'" in policy


def test_the_policy_refuses_inline_and_third_party_script(web) -> None:
    """An injected <script> must not run, and no code may come from another host."""
    policy = web.get("/").headers["content-security-policy"]
    script_src = next(d.strip() for d in policy.split(";") if d.strip().startswith("script-src"))

    assert "'unsafe-inline'" not in script_src, "inline script would defeat the point"
    assert script_src == "script-src 'self' 'unsafe-eval'", (
        "'unsafe-eval' is htmx's hx-on: handlers (new Function); anything more is a regression"
    )


def test_a_refused_cross_site_post_still_carries_the_headers(signed_in) -> None:
    response = signed_in.post("/logout", headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert "content-security-policy" in response.headers


# ---- draft ids from the URL ------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "1;drop", " 1", "1 ", "1e3"])
def test_a_draft_id_that_is_not_a_number_never_reaches_dspace(signed_in, fake_dspace, bad) -> None:
    """These do reach the handler -- routing only filters the slash-bearing ones --
    and each used to become a real DSpace round trip for an id it could not issue."""
    response = signed_in.get(f"/review/{bad}")

    assert response.status_code == 404, response.status_code
    assert not fake_dspace.called("get_workspaceitem")


@pytest.mark.parametrize(
    "bad", ["1/../../core/items/xyz", "../../core", "1%2F..%2Fcore", "", "-1", "٣"]
)
def test_the_validator_refuses_anything_that_is_not_a_plain_number(bad) -> None:
    """Unit-level, because routing 404s the slash-bearing ones before a handler is
    reached -- this is the guarantee itself, not the path that happens to reach it."""
    from scholardrop.dspace import DSpaceNotFound
    from scholardrop.routes.deps import draft_id

    with pytest.raises(DSpaceNotFound):
        draft_id(bad)


def test_an_ordinary_draft_id_still_works(signed_in, fake_dspace) -> None:
    assert signed_in.get("/review/42").status_code == 200
    assert fake_dspace.called("get_workspaceitem")
