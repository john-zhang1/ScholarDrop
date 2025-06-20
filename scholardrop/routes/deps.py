"""Shared route plumbing: sessions, per-request DSpace clients, page context."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from scholardrop.config import Instance
from scholardrop.dspace import DSpaceClient
from scholardrop.session import Session


class NeedsLogin(Exception):
    """No usable session -- send the user back to the repository picker."""

    def __init__(self, message: str = "Your session has ended. Please sign in again.") -> None:
        self.message = message
        super().__init__(message)


class InstanceNotFound(Exception):
    """A URL named an instance that is not configured."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"unknown repository {key!r}")


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def get_session(request: Request) -> Session | None:
    raw = request.cookies.get(request.app.state.cookie.name)
    session_id = request.app.state.cookie.loads(raw)
    if session_id is None:
        return None
    return request.app.state.sessions.get(session_id)


def require_session(request: Request) -> Session:
    session = get_session(request)
    if session is None or not session.authenticated:
        raise NeedsLogin()
    return session


def instance_for(request: Request, key: str) -> Instance:
    instance = request.app.state.registry.instance(key)
    if instance is None:
        raise InstanceNotFound(key)
    return instance


def session_instance(request: Request, session: Session) -> Instance:
    if not session.instance_key:
        raise NeedsLogin()
    return instance_for(request, session.instance_key)


@contextlib.asynccontextmanager
async def dspace_client(request: Request, session: Session) -> AsyncIterator[DSpaceClient]:
    """A client bound to this session's instance, for the life of one request.

    One client per request rather than a shared one: httpx keeps a cookie jar, and
    DSpace's CSRF check pairs the X-XSRF-TOKEN header with the DSPACE-XSRF-COOKIE
    cookie -- sharing a jar across users would cross their tokens.

    Tokens rotate constantly (CSRF on every response, the JWT on refresh), so
    whatever the client ends up holding is written back to the session on exit.
    """
    instance = session_instance(request, session)
    factory = request.app.state.client_factory
    client = factory(instance)
    try:
        client.restore(bearer=session.jwt, xsrf=session.xsrf_token)
        if session.eperson_name and client.eperson is None:
            # Saves a status() round trip just to label log lines.
            client.restore_eperson(uuid=session.eperson_uuid, name=session.eperson_name)
        yield client
    finally:
        session.jwt = client.bearer_token
        session.xsrf_token = client.xsrf_token
        request.app.state.sessions.save(session)
        await client.aclose()


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    """Context every template gets: who is signed in, and where."""
    session = get_session(request)
    instance = None
    if session and session.instance_key:
        instance = request.app.state.registry.instance(session.instance_key)
    context: dict[str, Any] = {
        "session": session,
        "instance": instance,
        "signed_in_as": session.eperson_name if session and session.authenticated else None,
    }
    context.update(extra)
    return context


def render(request: Request, template: str, /, **extra: Any) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, template, page_context(request, **extra)
    )
