"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scholardrop.config import Instance, Registry, load_registry
from scholardrop.dspace import (
    DSpaceAuthError,
    DSpaceClient,
    DSpaceForbidden,
    DSpaceNotFound,
    DSpaceUnavailable,
)
from scholardrop.extract import llm
from scholardrop.extract.backends import local_is_configured
from scholardrop.ratelimit import RateLimiter
from scholardrop.request_log import RequestContextMiddleware, configure_logging
from scholardrop.routes import auth, browse, drafts, home, review, submissions, submit
from scholardrop.routes.deps import InstanceNotFound, NeedsLogin, is_htmx, page_context
from scholardrop.security import SameOriginMiddleware, SecurityHeadersMiddleware
from scholardrop.session import (
    InMemorySessionStore,
    SessionCookie,
    SessionStore,
    SlidingSessionMiddleware,
)
from scholardrop.settings import Settings, load_settings
from scholardrop.submission.definition import ConfigCache
from scholardrop.submission.jobs import ExtractionJobs, Extractor, run_extraction
from scholardrop.submission.storage import sweep_uploads

_PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
STATIC_DIR = _PACKAGE_DIR / "static"

log = logging.getLogger("scholardrop")

ClientFactory = Callable[[Instance], DSpaceClient]


def create_app(
    settings: Settings | None = None,
    *,
    client_factory: ClientFactory | None = None,
    extractor: Extractor | None = None,
) -> FastAPI:
    """Build the app. Config is loaded here so a bad config fails at startup.

    `client_factory` is the seam for tests: it builds the DSpaceClient for an
    instance, one per request (see routes/deps.dspace_client). `extractor` is the
    same idea for the PDF pipeline, which route tests must not actually run.
    """
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    registry: Registry = load_registry(settings.config_dir)
    sessions: SessionStore = InMemorySessionStore(settings.session_ttl_seconds)
    cookie = SessionCookie(
        settings.secret_key,
        name=settings.session_cookie_name,
        secure=settings.session_cookie_secure,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI(title="ScholarDrop", version="1.0.0", lifespan=_lifespan)

    # Shared, request-independent state. Routes read it off request.app.state.
    app.state.settings = settings
    app.state.registry = registry
    app.state.sessions = sessions
    app.state.cookie = cookie
    app.state.templates = templates
    app.state.client_factory = client_factory or DSpaceClient.for_instance
    app.state.extractor = extractor or run_extraction
    app.state.extractions = ExtractionJobs()
    # Repository configuration (submission forms, vocabularies) -- shared, since
    # it is the same for every user of an instance.
    app.state.form_configs = ConfigCache(settings.config_cache_ttl_seconds)
    app.state.login_limiter = RateLimiter(
        settings.login_rate_limit_attempts, settings.login_rate_limit_window_seconds
    )

    # Outermost first: every line logged during a request carries its id, and a
    # forged cross-site POST is refused before any route sees it. The cookie
    # refresh is innermost -- it reads the session the route just used.
    app.add_middleware(SlidingSessionMiddleware)
    app.add_middleware(SameOriginMiddleware)
    # Outside the origin check, so even a refused request carries the headers.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(home.router)
    app.include_router(auth.router)
    app.include_router(browse.router)
    app.include_router(submit.router)
    app.include_router(review.router)
    app.include_router(drafts.router)
    app.include_router(submissions.router)
    _install_error_handlers(app)

    log.info(
        "loaded %d instance(s) and %d mapping profile(s) from %s",
        len(registry.instances),
        len(registry.profiles),
        settings.config_dir,
    )
    if settings.secret_key_was_generated:
        log.warning(
            "SCHOLARDROP_SECRET_KEY not set; using a per-process key -- "
            "sessions will not survive a restart"
        )
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Turn cross-cutting failures into pages, not stack traces.

    Session loss has to work for both a full page load and an HTMX partial: a
    partial gets `HX-Redirect` so the browser navigates instead of swapping a login
    page into a fragment of the collection browser.
    """

    def error_page(
        request: Request,
        *,
        heading: str,
        message: str,
        status: int,
        back_href: str = "/",
        back_label: str = "Start again",
    ) -> HTMLResponse:
        response: HTMLResponse = request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            page_context(
                request,
                heading=heading,
                message=message,
                back_href=back_href,
                back_label=back_label,
            ),
        )
        response.status_code = status
        return response

    @app.exception_handler(NeedsLogin)
    async def _needs_login(request: Request, exc: NeedsLogin) -> Response:
        if is_htmx(request):
            return Response(status_code=204, headers={"HX-Redirect": "/"})
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(request.app.state.cookie.name, path="/")
        return response

    @app.exception_handler(DSpaceAuthError)
    async def _auth_error(request: Request, exc: DSpaceAuthError) -> Response:
        # The token expired mid-flow and could not be refreshed: drop the session
        # and send the user back to sign in.
        session_id = request.app.state.cookie.loads(
            request.cookies.get(request.app.state.cookie.name)
        )
        if session_id:
            request.app.state.sessions.delete(session_id)
        log.info("session ended mid-request: %s", exc)
        return await _needs_login(request, NeedsLogin())

    @app.exception_handler(InstanceNotFound)
    async def _no_instance(request: Request, exc: InstanceNotFound) -> Response:
        return error_page(
            request,
            heading="Unknown repository",
            message=f"There is no repository configured with the key “{exc.key}”.",
            status=404,
            back_label="Choose a repository",
        )

    @app.exception_handler(DSpaceUnavailable)
    async def _unavailable(request: Request, exc: DSpaceUnavailable) -> Response:
        log.error("repository unavailable: %s", exc)
        return error_page(
            request,
            heading="The repository is not responding",
            message=(
                "ScholarDrop could not reach the repository. Nothing was lost — "
                "try again in a moment."
            ),
            status=502,
            back_href="/browse",
            back_label="Try again",
        )

    @app.exception_handler(DSpaceNotFound)
    async def _not_found(request: Request, exc: DSpaceNotFound) -> Response:
        # Usually a draft that was discarded, deposited, or removed in the DSpace
        # UI, reached again from history or a stale HTMX swap.
        log.info("gone: %s", exc)
        return error_page(
            request,
            heading="That is no longer in the repository",
            message=(
                "The draft you asked for has been discarded, submitted, or removed. "
                "Your other submissions in progress are listed under Drafts."
            ),
            status=404,
            back_href="/drafts",
            back_label="Your drafts",
        )

    @app.exception_handler(DSpaceForbidden)
    async def _forbidden(request: Request, exc: DSpaceForbidden) -> Response:
        # A 403 that no route turned into a specific access-denied screen.
        log.info("forbidden: %s", exc)
        return error_page(
            request,
            heading="The repository refused that",
            message=(
                "Your account does not have permission for this action"
                + (f" ({exc.resource})." if exc.resource else ".")
            ),
            status=403,
            back_href="/browse",
            back_label="Back to collections",
        )


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    background = [asyncio.create_task(_sweep(app))]
    if _should_warm_extraction(app):
        background.append(asyncio.create_task(_warm_extraction(app)))
    if _should_warm_local(app):
        background.append(asyncio.create_task(_warm_local(app)))
    # One connection pool for every ORCID lookup, instead of a TLS handshake per
    # keystroke. The route falls back to a per-request client when this is absent
    # (a test app whose lifespan never ran), so nothing depends on it existing.
    app.state.orcid_http = httpx.AsyncClient(timeout=app.state.settings.orcid_timeout_seconds)
    try:
        yield
    finally:
        await app.state.orcid_http.aclose()
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _should_warm_extraction(app: FastAPI) -> bool:
    """Only worth a request if the cloud tier can actually run for someone."""
    settings: Settings = app.state.settings
    registry: Registry = app.state.registry
    return bool(
        settings.extraction_warm_on_startup
        and settings.anthropic_api_key
        and any(instance.extraction == "cloud" for instance in registry.instances.values())
    )


def _should_warm_local(app: FastAPI) -> bool:
    """Warm the local weights when anyone here can reach them.

    That is any instance configured for local extraction, and also any cloud
    instance -- a submitter there can tick "restricted content" at upload and land
    on the local backend without warning (DESIGN.md §5).
    """
    settings: Settings = app.state.settings
    registry: Registry = app.state.registry
    return bool(
        settings.local_llm_warm_on_startup
        and local_is_configured(settings)
        and any(
            instance.extraction in {"local", "cloud"} for instance in registry.instances.values()
        )
    )


async def _warm_local(app: FastAPI) -> None:
    """Load the local model into RAM before an embargoed submission waits for it."""
    settings: Settings = app.state.settings
    try:
        await llm.warm_local(settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # nothing may be listening; that is a log line, not a fault
        log.info(
            "local model warm-up did not complete (%s: %s); "
            "restricted submissions will fall back to heuristics if it stays down",
            type(exc).__name__,
            exc,
        )
    else:
        log.info("local model warm-up complete (%s)", settings.local_llm_model)


async def _warm_extraction(app: FastAPI) -> None:
    """Pay for the output schema's grammar compile before a submission has to.

    Runs alongside startup rather than blocking it: the app serves requests while
    this is in flight, and a failure costs nothing beyond a log line -- the first
    real extraction just pays the cold cost itself, as it did before.
    """
    try:
        await llm.warm_grammar(app.state.settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a warm-up must never take the app down
        log.info("extraction warm-up did not complete (%s: %s)", type(exc).__name__, exc)
    else:
        log.info("extraction warm-up complete; tier 1 grammar is compiled")


async def _sweep(app: FastAPI) -> None:
    """Drop expired sessions and stale rate-limit entries on a timer.

    One iteration's failure must not end the loop: this task is the only thing
    that removes uploaded theses nobody cleaned up, and if it dies silently they
    sit on disk until the process is restarted. So a bad sweep costs one cycle
    and a log line -- an unreadable upload directory, a filesystem that went away
    under a shared mount -- and the next tick tries again.
    """
    settings: Settings = app.state.settings
    sessions: SessionStore = app.state.sessions
    registry: Registry = app.state.registry
    longest_session_ttl = max(
        [settings.session_ttl_seconds]
        + [i.session_ttl(settings.session_ttl_seconds) for i in registry.instances.values()]
    )
    while True:
        await asyncio.sleep(settings.session_sweep_interval_seconds)
        try:
            removed = sessions.sweep()
            app.state.login_limiter.sweep()
            # An extraction result outlives its session by nothing: once the session
            # is gone, no one can ask for it again. Ask the store which sessions are
            # still live rather than going on age, or a user who took longer than the
            # TTL to open the review form would lose their pre-fill with no notice.
            app.state.extractions.sweep(
                # The *longest* TTL any instance configures. This argument is only
                # the fallback for jobs whose owner cannot be resolved, and sweeping
                # one repository's results on another's shorter clock is the same
                # silent un-prefill the liveness check exists to prevent.
                longest_session_ttl,
                owner_is_live=lambda owner: sessions.get(owner, touch=False) is not None,
            )
            # Expired repository configuration. Reads drop stale entries anyway;
            # this keeps ones nobody asks for again from sitting there.
            app.state.form_configs.sweep()
            # The backstop for uploads nobody cleaned up -- an abandoned tab, a crash
            # between upload and deposit (DESIGN.md §8: purged on session end/TTL).
            sweep_uploads(settings.upload_dir, settings.upload_ttl_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("sweep failed, continuing (%s: %s)", type(exc).__name__, exc)
            continue
        if removed:
            log.info("swept %d expired session(s)", removed)


app_factory = create_app
