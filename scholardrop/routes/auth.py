"""Sign in and out (DESIGN.md §6 steps 1-2).

Credentials are read from the form, handed to DSpace once, and dropped. What is
kept -- server-side, keyed by the signed cookie -- is the JWT and CSRF token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from scholardrop.dspace import DSpaceAuthError, DSpaceUnavailable
from scholardrop.routes.deps import get_session, instance_for, render
from scholardrop.submission.storage import discard_upload_dir

router = APIRouter()
log = logging.getLogger("scholardrop.auth")


@router.get("/login/{instance_key}", response_class=HTMLResponse)
async def login_form(request: Request, instance_key: str) -> HTMLResponse:
    instance = instance_for(request, instance_key)
    return render(request, "login.html", target=instance)


@router.post("/login/{instance_key}", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    instance_key: str,
    user: str = Form(""),
    password: str = Form(""),
) -> Response:
    instance = instance_for(request, instance_key)
    limiter = request.app.state.login_limiter
    key = f"{instance_key}:{_client_ip(request)}"

    def form_error(message: str, status: int = 400) -> HTMLResponse:
        response = render(request, "login.html", target=instance, error=message, user=user)
        response.status_code = status
        return response

    if not limiter.allowed(key):
        wait = limiter.retry_after(key)
        log.warning("login rate limit hit for %s", key)
        return form_error(
            f"Too many failed sign-in attempts. Try again in {wait} seconds.", status=429
        )

    if not user or not password:
        return form_error("Enter your username and password.")

    client = request.app.state.client_factory(instance)
    # Closed on every path. A failed sign-in used to return without closing it, and
    # failed sign-ins are exactly what an attacker generates in bulk -- each one
    # leaking a connection pool.
    try:
        # The password is handed to DSpace here and referenced nowhere else: it is
        # not stored on the session, the client, or in any log line.
        try:
            status = await client.login(user, password)
        except DSpaceAuthError:
            limiter.record_failure(key)
            log.info("failed sign-in attempt for %s", key)
            # Deliberately vague: don't reveal whether the account exists.
            return form_error("Those credentials were not accepted by the repository.", status=401)
        except DSpaceUnavailable as exc:
            log.error("repository unreachable during sign-in: %s", exc)
            return form_error(
                f"{instance.name} could not be reached. Please try again shortly.", status=502
            )

        if not status.authenticated:
            limiter.record_failure(key)
            return form_error("The repository did not complete the sign-in.", status=401)

        limiter.reset(key)
        sessions = request.app.state.sessions
        # A fresh session id on sign-in, so a pre-login cookie cannot be reused.
        old = get_session(request)
        if old is not None:
            sessions.delete(old.id)

        eperson = status.eperson
        session = sessions.create(
            instance_key=instance.key,
            jwt=client.bearer_token,
            xsrf_token=client.xsrf_token,
            eperson_name=eperson.display_name if eperson else None,
            eperson_uuid=eperson.uuid if eperson else None,
        )
        log.info("signed in %s at %s", session.eperson_name, instance.key)

        response = RedirectResponse("/browse", status_code=303)
        _set_session_cookie(request, response, session.id)
        return response
    finally:
        await client.aclose()


@router.post("/logout")
async def logout(request: Request) -> Response:
    session = get_session(request)
    if session is not None:
        # Best effort at the DSpace end; the local session goes regardless.
        if session.jwt and session.instance_key:
            instance = request.app.state.registry.instance(session.instance_key)
            if instance is not None:
                client = request.app.state.client_factory(instance)
                client.restore(bearer=session.jwt, xsrf=session.xsrf_token)
                try:
                    await client.logout()
                finally:
                    await client.aclose()
        # The uploaded PDF has no reason to outlive the session that produced it
        # (DESIGN.md §8); the sweep is only the backstop for sessions that end
        # without anyone saying so.
        discard_upload_dir(request.app.state.settings.upload_dir, session)
        request.app.state.extractions.forget_owner(session.id)
        request.app.state.sessions.delete(session.id)
        log.info("signed out %s", session.eperson_name)

    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(request.app.state.cookie.name, path="/")
    return response


def _set_session_cookie(request: Request, response: Response, session_id: str) -> None:
    settings = request.app.state.settings
    cookie = request.app.state.cookie
    response.set_cookie(
        cookie.name,
        cookie.dumps(session_id),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=cookie.secure,
        samesite="lax",
        path="/",
    )


def _client_ip(request: Request) -> str:
    """Client identity for rate limiting, from the connection rather than a header.

    Reading `X-Forwarded-For` here would be worse than useless. nginx appends to it
    (`$proxy_add_x_forwarded_for`), so a client that sends its own value keeps the
    left-hand entries -- and trusting any of them lets an attacker rotate the header
    to get unlimited password attempts, or spoof someone else's address to lock them
    out. The header is attacker-controlled data, not a client identity.

    `request.client` is the resolved one. Uvicorn's ProxyHeadersMiddleware
    (`--proxy-headers --forwarded-allow-ips=127.0.0.1` in deploy/scholardrop.service)
    rewrites it from X-Forwarded-For *only* when the peer is the trusted proxy, and
    takes the right-most untrusted entry -- the one nginx appended, which the client
    cannot forge.
    """
    return request.client.host if request.client else "unknown"
