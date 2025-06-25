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

# Content-Security-Policy. Every asset ScholarDrop serves is same-origin -- one
# stylesheet, one vendored script, no fonts, no images, no CDN -- so everything is
# `'self'` and injected markup cannot pull code in from anywhere.
#
# `'unsafe-eval'` is the one concession, and it is htmx's: `hx-on:` handlers are
# compiled with `new Function`, so without it the tab switcher and the "remove
# this row" button stop working silently. It is far weaker than it sounds here --
# `script-src 'self'` still refuses inline `<script>`, so an injected script tag
# does not run, and nothing attacker-controlled reaches eval: htmx only evaluates
# attributes written into the templates, and Jinja2 autoescaping keeps user data
# from becoming an attribute in the first place.
#
# Kept in the app rather than the nginx block because it describes what these
# pages need, and because a policy nobody can test is how you ship a broken one.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-eval'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # Belt and braces with frame-ancestors, for anything that predates CSP.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the response headers that constrain what a page may do."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            # nginx may set some of these too; do not send them twice.
            response.headers.setdefault(header, value)
        return response


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
