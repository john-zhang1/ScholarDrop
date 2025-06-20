"""Request-scoped logging (DESIGN.md §7).

Every log line carries the id of the request that produced it, so a user's report
("it failed at about 10:40") can be turned into the whole story of one request --
including the DSpace calls it made, which are logged by `dspace/client.py`.

What is never logged, here or anywhere: passwords, JWTs, CSRF tokens, session ids,
cookie values, or request bodies. The access line is method, path, status,
duration, and the signed-in EPerson -- nothing that could be replayed.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("scholardrop.access")

REQUEST_ID_HEADER = "X-Request-ID"
MAX_INBOUND_ID = 64

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Paths not worth a line each; they are static and say nothing about behaviour.
QUIET_PREFIXES = ("/static/",)


def current_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Puts `request_id` on every record, so the format string can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def _inbound_id(request: Request) -> str | None:
    """A request id from the proxy, if it looks like one.

    It ends up in log lines, so it is length-capped and restricted to characters
    that cannot forge a line -- an attacker-supplied header must not be able to
    inject a newline into the log.
    """
    raw = request.headers.get(REQUEST_ID_HEADER)
    if not raw:
        return None
    candidate = raw.strip()[:MAX_INBOUND_ID]
    if candidate and all(c.isalnum() or c in "-_." for c in candidate):
        return candidate
    return None


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log one line per request, echo the id back."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _inbound_id(request) or secrets.token_hex(6)
        token = _request_id.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Logged here because the handler that turns this into a 500 sees no
            # timing, and an unhandled error is the one you most want to find.
            log.exception(
                "%s %s -> unhandled exception after %dms",
                request.method,
                request.url.path,
                int((time.perf_counter() - started) * 1000),
            )
            _request_id.reset(token)
            raise

        elapsed = int((time.perf_counter() - started) * 1000)
        if not request.url.path.startswith(QUIET_PREFIXES):
            log.info(
                "%s %s -> %d %dms [%s]",
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
                _who(request),
            )
        response.headers[REQUEST_ID_HEADER] = request_id
        _request_id.reset(token)
        return response


def _who(request: Request) -> str:
    """The signed-in EPerson, never the session id that identifies them."""
    try:
        from scholardrop.routes.deps import get_session

        session = get_session(request)
    except Exception:  # logging must not be able to break a response
        return "anonymous"
    if session is None or not session.authenticated:
        return "anonymous"
    return session.eperson_name or session.eperson_uuid or "signed in"


def configure_logging(level: str) -> None:
    """Root logging with the request id in every line."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
    )
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
