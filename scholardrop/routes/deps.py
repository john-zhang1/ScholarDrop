"""Shared route plumbing: sessions, per-request DSpace clients, page context."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from scholardrop.config import Instance
from scholardrop.dspace import DSpaceClient, DSpaceNotFound
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


def draft_id(wsi_id: str) -> str:
    """A workspaceitem id from a URL, checked before it reaches a DSpace path.

    Every client method interpolates this into a path (`/submission/workspaceitems/
    {id}`) and `urljoin` resolves `..`, so an id is not something to pass through
    unexamined. Routing already turns the obvious `1/../../core/items/x` into a 404
    before a handler sees it (measured, not assumed) -- what actually arrives here
    is junk like `abc`, `1;drop` or ` 1`, which used to become a real DSpace round
    trip and a 500-shaped answer. DSpace ids are integers; anything else stops here.

    Raised as `DSpaceNotFound` because that is what it means to the person who
    followed a mangled link, and it already renders the right page.
    """
    # `isascii()` matters: str.isdigit() is true of "٣" and "²" too, and those are
    # not ids DSpace could have issued.
    if not (wsi_id.isascii() and wsi_id.isdigit()):
        raise DSpaceNotFound(
            "that draft id is not one this repository could have issued",
            path=f"/submission/workspaceitems/{wsi_id}",
        )
    return wsi_id


# Every route that takes a draft id from the URL declares it as this, so the check
# is one word at the point of entry rather than a line each handler can forget.
DraftId = Annotated[str, Depends(draft_id)]


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


def release_draft(request: Request, session: Session, wsi_id: str) -> None:
    """Let go of everything this process holds for one draft.

    Every exit from the flow -- deposited, discarded, cancelled after a duplicate
    warning -- ends here, so that "an uploaded thesis is removed the moment it has
    no further purpose" is one implementation rather than one per route. It does
    **not** touch DSpace: whether the draft itself survives is the caller's
    decision, and after a deposit it must.

    Only this draft's file goes. A session can be part-way through several in
    several tabs, and taking the wrong one leaves another tab pointing at a file
    that is no longer there.
    """
    request.app.state.extractions.forget(session.id, wsi_id)
    if session.workspaceitem_id == wsi_id:
        session.workspaceitem_id = None
    stored = session.forget_submission(wsi_id).get("path")
    if stored:
        Path(stored).unlink(missing_ok=True)
    request.app.state.sessions.save(session)


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
