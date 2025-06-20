"""Cross-site request forgery defence for ScholarDrop's own forms (DESIGN.md §8).

The session cookie is `SameSite=Lax`, which already stops a cross-site form POST
from carrying it. This middleware is the second lock: an unsafe request whose
`Origin` (or `Referer`) names a different host is refused outright.

Why both: SameSite is enforced by the browser, so it protects exactly as well as
the browser the user happens to be running. The header check is enforced here.
Neither can be bypassed by the other's failure, and both are cheap.

A request with **no** Origin and no Referer is allowed. Browsers send Origin on
every form POST, but `curl`, the test client, and health checks do not, and
refusing them would break the tooling without protecting anyone: forgery needs a
browser, and a browser tells us where it came from.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

log = logging.getLogger("scholardrop.security")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Refuse state-changing requests that came from another site."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method.upper() not in SAFE_METHODS:
            source = _source_host(request)
            expected = _expected_host(request)
            if source is not None and source != expected:
                log.warning(
                    "cross-origin %s %s from %r refused", request.method, request.url.path, source
                )
                return PlainTextResponse(
                    "This request did not come from ScholarDrop. Reload the page and try again.",
                    status_code=403,
                )
        return await call_next(request)


def _source_host(request: Request) -> str | None:
    """Where the browser says the request came from, host:port and all."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return urlsplit(origin).netloc.lower() or None
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer).netloc.lower() or None
    return None


def _expected_host(request: Request) -> str:
    """The host the browser used to reach us.

    Behind the nginx of DESIGN.md §8 this is the `Host` header the proxy passes
    through, which is why the deployment docs insist on `proxy_set_header Host`.
    """
    return (request.headers.get("host") or request.url.netloc).lower()
