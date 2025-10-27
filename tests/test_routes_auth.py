"""Login, session handling, and logout (DESIGN.md §6 steps 1-2)."""

from __future__ import annotations

import pytest

from scholardrop.dspace import DSpaceAuthError, DSpaceUnavailable
from tests.conftest import LOGIN_PATH, SHIPPED_INSTANCE_KEY
from tests.fakes import page_of


def test_login_form_names_the_instance(web, app) -> None:
    response = web.get(LOGIN_PATH)

    assert response.status_code == 200
    # The registry's display name, not a literal: what is being tested is that the
    # form names whichever instance you are signing in to.
    assert app.state.registry.instance(SHIPPED_INSTANCE_KEY).name in response.text
    assert 'name="user"' in response.text
    assert 'type="password"' in response.text


def test_login_form_unknown_instance_is_404(web) -> None:
    response = web.get("/login/nosuchplace")

    assert response.status_code == 404
    assert "nosuchplace" in response.text


def test_successful_login_sets_session_and_redirects(web, app, fake_dspace) -> None:
    response = web.post(LOGIN_PATH, data={"user": "jane@example.edu", "password": "pw"})

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    cookie_name = app.state.settings.session_cookie_name
    assert cookie_name in response.cookies or cookie_name in web.cookies

    assert len(app.state.sessions) == 1
    session = next(iter(app.state.sessions._sessions.values()))
    assert session.instance_key == SHIPPED_INSTANCE_KEY
    assert session.jwt == "jwt-from-login"
    assert session.xsrf_token == "xsrf-from-login"
    assert session.eperson_name == "Jane Doe"


def test_session_cookie_is_signed_httponly_and_lax(web, app) -> None:
    response = web.post(LOGIN_PATH, data={"user": "jane@example.edu", "password": "pw"})

    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("Lax", "lax")
    session_id = next(iter(app.state.sessions._sessions))
    # The raw id must not be readable from the cookie.
    assert session_id not in set_cookie


def test_signed_in_banner_shows_display_name(signed_in, app) -> None:
    response = signed_in.get("/browse")

    assert response.status_code == 200
    assert "Jane Doe" in response.text
    assert app.state.registry.instance(SHIPPED_INSTANCE_KEY).name in response.text
    assert "Sign out" in response.text


def test_bad_credentials_render_inline_error(web, app, fake_dspace) -> None:
    fake_dspace.login_ok = False

    response = web.post(LOGIN_PATH, data={"user": "jane@example.edu", "password": "wrong"})

    assert response.status_code == 401
    assert "not accepted" in response.text
    # The attempted username is preserved; the password is not echoed anywhere.
    assert "jane@example.edu" in response.text
    assert "wrong" not in response.text
    assert len(app.state.sessions) == 0


def test_missing_fields_do_not_reach_dspace(web, fake_dspace) -> None:
    response = web.post(LOGIN_PATH, data={"user": "", "password": ""})

    assert response.status_code == 400
    assert not fake_dspace.called("login")


def test_unreachable_repository_says_so(web, fake_dspace) -> None:
    fake_dspace.login_error = DSpaceUnavailable("connection refused")

    response = web.post(LOGIN_PATH, data={"user": "jane@example.edu", "password": "pw"})

    assert response.status_code == 502
    assert "could not be reached" in response.text


def test_login_that_does_not_authenticate_is_rejected(web, app, fake_dspace) -> None:
    fake_dspace.authenticated_after_login = False

    response = web.post(LOGIN_PATH, data={"user": "jane@example.edu", "password": "pw"})

    assert response.status_code == 401
    assert len(app.state.sessions) == 0


# ---- rate limiting ---------------------------------------------------------


def test_failed_attempts_are_rate_limited(web, app, fake_dspace) -> None:
    fake_dspace.login_ok = False
    limit = app.state.settings.login_rate_limit_attempts

    for _ in range(limit):
        assert web.post(LOGIN_PATH, data={"user": "j", "password": "x"}).status_code == 401

    blocked = web.post(LOGIN_PATH, data={"user": "j", "password": "x"})

    assert blocked.status_code == 429
    assert "Too many failed sign-in attempts" in blocked.text


def test_rate_limit_does_not_call_dspace(web, app, fake_dspace) -> None:
    fake_dspace.login_ok = False
    for _ in range(app.state.settings.login_rate_limit_attempts):
        web.post(LOGIN_PATH, data={"user": "j", "password": "x"})
    fake_dspace.calls.clear()

    web.post(LOGIN_PATH, data={"user": "j", "password": "x"})

    assert not fake_dspace.called("login")


def test_successful_login_resets_the_limiter(web, app, fake_dspace) -> None:
    fake_dspace.login_ok = False
    for _ in range(app.state.settings.login_rate_limit_attempts - 1):
        web.post(LOGIN_PATH, data={"user": "j", "password": "x"})

    fake_dspace.login_ok = True
    assert web.post(LOGIN_PATH, data={"user": "j", "password": "pw"}).status_code == 303

    fake_dspace.login_ok = False
    assert web.post(LOGIN_PATH, data={"user": "j", "password": "x"}).status_code == 401


# ---- session lifecycle -----------------------------------------------------


def test_logout_clears_the_session_server_side(signed_in, app, fake_dspace) -> None:
    assert len(app.state.sessions) == 1

    response = signed_in.post("/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert len(app.state.sessions) == 0
    assert fake_dspace.called("logout"), "DSpace should be told too, best effort"


def test_protected_routes_redirect_when_not_signed_in(web) -> None:
    for path in ("/browse", "/browse/mine", "/submit"):
        response = web.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/"


def test_htmx_partial_gets_hx_redirect_not_a_page(web) -> None:
    response = web.get("/browse/mine/results", headers={"HX-Request": "true"})

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/"


def test_expired_session_is_rejected(signed_in, app) -> None:
    for session in list(app.state.sessions._sessions.values()):
        session.last_seen -= app.state.settings.session_ttl_seconds + 1

    response = signed_in.get("/browse")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_tampered_cookie_is_rejected(web, app) -> None:
    web.cookies.set(app.state.settings.session_cookie_name, "not-a-signed-value")

    response = web.get("/browse")

    assert response.status_code == 303


def test_token_expiry_mid_request_drops_the_session(signed_in, app, fake_dspace) -> None:
    fake_dspace.raise_on["find_submit_authorized"] = DSpaceAuthError("token expired")

    response = signed_in.get("/browse/mine/results")

    assert response.status_code == 303
    assert len(app.state.sessions) == 0, "an unrecoverable token failure ends the session"


def test_rotated_tokens_are_written_back_to_the_session(signed_in, app, fake_dspace) -> None:
    """CSRF tokens rotate on every DSpace response; the session must follow.

    Without the write-back, the next request rehydrates a client with a stale token
    and every modifying call 403s.
    """
    session = next(iter(app.state.sessions._sessions.values()))

    async def rotates_tokens(query=None, page=0, size=20):
        fake_dspace._xsrf = "rotated-token"
        fake_dspace._bearer = "refreshed-jwt"
        return page_of([])

    fake_dspace.find_submit_authorized = rotates_tokens  # type: ignore[method-assign]

    signed_in.get("/browse/mine/results")

    assert session.xsrf_token == "rotated-token"
    assert session.jwt == "refreshed-jwt"


def test_a_failed_sign_in_closes_its_client(web, fake_dspace) -> None:
    """Failed sign-ins are what an attacker generates in bulk; each one used to
    return without closing the client and leak its connection pool."""
    fake_dspace.login_ok = False

    response = web.post(LOGIN_PATH, data={"user": "j", "password": "wrong"})

    assert response.status_code == 401
    assert fake_dspace.closed, "the client was left open"


def test_an_unreachable_repository_closes_its_client(web, fake_dspace) -> None:
    from scholardrop.dspace import DSpaceUnavailable

    fake_dspace.login_error = DSpaceUnavailable("connection refused", path="/authn/login")

    response = web.post(LOGIN_PATH, data={"user": "j", "password": "pw"})

    assert response.status_code == 502
    assert fake_dspace.closed, "the client was left open"


def test_repeated_failures_are_rate_limited(web, app, fake_dspace) -> None:
    fake_dspace.login_ok = False
    for _ in range(app.state.settings.login_rate_limit_attempts):
        web.post(LOGIN_PATH, data={"user": "j", "password": "x"})

    blocked = web.post(LOGIN_PATH, data={"user": "j", "password": "x"})

    assert blocked.status_code == 429


@pytest.mark.parametrize("header", ["1.2.3.4", "1.2.3.4, 10.0.0.1", "9.9.9.9"])
def test_a_forged_forwarded_for_does_not_reset_the_rate_limit(
    web, app, fake_dspace, header: str
) -> None:
    """X-Forwarded-For is attacker-controlled, so it must not key the limiter.

    nginx *appends* to this header (`$proxy_add_x_forwarded_for`), so whatever the
    client sent stays in it. If any of those entries chose the bucket, rotating the
    header would buy unlimited password attempts -- and spoofing someone else's
    address would lock them out of signing in.
    """
    fake_dspace.login_ok = False
    for _ in range(app.state.settings.login_rate_limit_attempts):
        web.post(LOGIN_PATH, data={"user": "j", "password": "x"})

    still_blocked = web.post(
        LOGIN_PATH,
        data={"user": "j", "password": "x"},
        headers={"X-Forwarded-For": header},
    )

    assert still_blocked.status_code == 429
