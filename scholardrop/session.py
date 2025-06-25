"""Server-side sessions.

The browser only ever holds a signed, HttpOnly cookie containing an opaque
session id. Everything sensitive -- the DSpace JWT, the CSRF token -- lives in
this process, keyed by that id (DESIGN.md §2, §8).

SessionStore is the seam: swapping InMemorySessionStore for a Redis-backed
implementation is the only change needed to run multi-worker.
"""

from __future__ import annotations

import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


@dataclass
class Session:
    """Server-side state for one signed-in browser."""

    id: str
    created_at: float
    last_seen: float
    instance_key: str | None = None
    # Set once the user authenticates. Never leaves the process (DESIGN.md §8).
    jwt: str | None = None
    xsrf_token: str | None = None
    eperson_name: str | None = None
    eperson_uuid: str | None = None
    collection_uuid: str | None = None
    workspaceitem_id: str | None = None
    # Scratch space for phase-specific state (upload paths, extraction results).
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.jwt is not None

    def submission(self, workspaceitem_id: str | int) -> dict[str, Any]:
        """Scratch space belonging to **one** draft. Read-only; never creates.

        Anything about a specific submission -- the uploaded file's name and path --
        has to be keyed by workspaceitem, not held in one slot per session. Two tabs
        are two drafts: with a single slot, depositing one deletes the other's
        uploaded file and the pages label it with the wrong filename.

        Reads must not write, or hitting /review/{id} with invented ids would grow
        this dict for the life of the session. A resumed draft legitimately has no
        entry, so an empty one is a normal answer.
        """
        submissions = self.data.get("submissions") or {}
        return submissions.get(str(workspaceitem_id), {})

    def remember_submission(self, workspaceitem_id: str | int, **values: Any) -> None:
        """Record what this session knows about one draft."""
        submissions = self.data.setdefault("submissions", {})
        submissions.setdefault(str(workspaceitem_id), {}).update(values)

    def forget_submission(self, workspaceitem_id: str | int) -> dict[str, Any]:
        """Drop one draft's scratch space and hand it back, for cleanup."""
        submissions = self.data.get("submissions")
        if not submissions:
            return {}
        return submissions.pop(str(workspaceitem_id), {})

    def expires_at(self, ttl_seconds: int) -> float:
        return self.last_seen + ttl_seconds

    def is_expired(self, ttl_seconds: int, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at(ttl_seconds)


class SessionStore(ABC):
    """Create / fetch / drop sessions, with idle expiry.

    `ttl_seconds` is an *idle* timeout: get() refreshes last_seen, so an active
    user is never logged out mid-flow, and an abandoned tab expires on schedule.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds

    @abstractmethod
    def create(self, **initial: Any) -> Session: ...

    @abstractmethod
    def get(self, session_id: str, *, touch: bool = True) -> Session | None: ...

    @abstractmethod
    def save(self, session: Session) -> None: ...

    @abstractmethod
    def delete(self, session_id: str) -> None: ...

    @abstractmethod
    def sweep(self) -> int:
        """Drop every expired session. Returns how many were removed."""

    @abstractmethod
    def __len__(self) -> int: ...


class InMemorySessionStore(SessionStore):
    """Single-process dict store. Fine for v1 (one uvicorn worker)."""

    def __init__(self, ttl_seconds: int, *, clock: Any = time.monotonic) -> None:
        super().__init__(ttl_seconds)
        self._sessions: dict[str, Session] = {}
        self._clock = clock

    def create(self, **initial: Any) -> Session:
        now = self._clock()
        session = Session(id=secrets.token_urlsafe(32), created_at=now, last_seen=now, **initial)
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str, *, touch: bool = True) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = self._clock()
        if session.is_expired(self.ttl_seconds, now):
            del self._sessions[session_id]
            return None
        if touch:
            session.last_seen = now
        return session

    def save(self, session: Session) -> None:
        # In-memory sessions are mutated in place; save() exists so callers write
        # code that also works against a Redis store.
        self._sessions[session.id] = session

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def sweep(self) -> int:
        now = self._clock()
        expired = [sid for sid, s in self._sessions.items() if s.is_expired(self.ttl_seconds, now)]
        for sid in expired:
            del self._sessions[sid]
        return len(expired)

    def __len__(self) -> int:
        return len(self._sessions)


class SessionCookie:
    """Signs and verifies the session id carried in the cookie."""

    def __init__(
        self, secret_key: str, *, name: str, secure: bool, salt: str = "scholardrop"
    ) -> None:
        self.name = name
        self.secure = secure
        self._serializer = URLSafeSerializer(secret_key, salt=salt)

    def dumps(self, session_id: str) -> str:
        return self._serializer.dumps(session_id)

    def loads(self, raw: str | None) -> str | None:
        """Session id from a cookie value, or None if absent or tampered with."""
        if not raw:
            return None
        try:
            value = self._serializer.loads(raw)
        except BadSignature:
            return None
        return value if isinstance(value, str) else None


class SlidingSessionMiddleware(BaseHTTPMiddleware):
    """Re-issue the session cookie on every request that used a live session.

    The server-side TTL is an *idle* timeout -- get() refreshes last_seen, so an
    active user's session never expires. The cookie has to slide with it, or the
    browser drops it a fixed TTL after sign-in no matter how busy the user was:
    someone filling in a long review form would post to /confirm with no cookie and
    lose every edit they had typed, because the form lives in the page until deposit.

    Only a request that arrived with a valid, unexpired session is refreshed, and
    never one whose response already sets the cookie itself -- that is sign-in
    (minting a new id) or sign-out (deleting it), and neither wants a second opinion.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        state = request.app.state
        cookie: SessionCookie = state.cookie

        if _already_sets(response, cookie.name):
            return response
        session_id = cookie.loads(request.cookies.get(cookie.name))
        if session_id is None:
            return response
        # touch=False: the route has already refreshed last_seen if it looked at the
        # session. This must not keep a session alive on static-asset traffic alone.
        if state.sessions.get(session_id, touch=False) is None:
            return response

        response.set_cookie(
            cookie.name,
            cookie.dumps(session_id),
            max_age=state.settings.session_ttl_seconds,
            httponly=True,
            secure=cookie.secure,
            samesite="lax",
            path="/",
        )
        return response


def _already_sets(response: Response, name: str) -> bool:
    return any(
        header.split("=", 1)[0].strip() == name for header in response.headers.getlist("set-cookie")
    )
