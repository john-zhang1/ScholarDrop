"""A real browser against a real server (Phase 6b).

Everything else in this suite asserts the HTML ScholarDrop *sends*. This asserts
what a browser *does* with it: htmx swaps, the polled progress state, the lazy
tree, `hx-on:` handlers (which need the CSP to permit `unsafe-eval`, so they fail
here first if that is ever narrowed), and the no-JavaScript fallbacks.

Two things make it runnable without a repository or an API key:

- the server is the real app, wired to the same `FakeDSpaceClient` the route tests
  use through `client_factory` — so this exercises ScholarDrop's behaviour, not
  DSpace's;
- the browser is the system Chromium, driven through Playwright with its own
  download skipped, because this VM has Chromium and no wish for a second copy.

Skipped unless `--e2e` is passed, so `pytest -q` on a box with no browser is
unaffected.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import LOGIN_PATH

# The system browsers, in preference order. headless_shell is the smaller one and
# is what the DSpace work on this VM already uses.
CHROMIUM_PATHS = (
    "/usr/lib64/chromium-browser/headless_shell",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
)


def chromium_path() -> str | None:
    return next((p for p in CHROMIUM_PATHS if Path(p).exists()), None)


@pytest.fixture(scope="session")
def browser():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    executable = chromium_path()
    if executable is None:
        pytest.skip(f"no system chromium; looked in {', '.join(CHROMIUM_PATHS)}")

    with playwright.sync_playwright() as p:
        # --no-sandbox because this runs unprivileged in a VM with no user
        # namespaces; nothing untrusted is ever loaded.
        instance = p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def live_app(app, fake_dspace) -> Any:
    """The real app on a real port, in a thread, torn down after the test."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("the app did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def page(browser, live_app, request):
    """A page with console errors captured, so a silent JS failure is not silent.

    A broken `hx-on:` handler or a CSP refusal does not fail a request or change
    the HTML; it just stops working. Every test asserts the console stayed clean
    unless it says otherwise (`@pytest.mark.allow_console`).
    """
    javascript = "no_js" not in request.keywords
    context = browser.new_context(java_script_enabled=javascript)
    page = context.new_page()
    page.errors: list[str] = []  # type: ignore[attr-defined]
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    try:
        yield page
    finally:
        if "allow_console" not in request.keywords:
            assert page.errors == [], f"the browser logged errors: {page.errors}"
        context.close()


@pytest.fixture
def signed_in_page(page, live_app, fake_dspace):
    """Signed in through the real form, so the cookie is the real cookie."""
    page.goto(f"{live_app}{LOGIN_PATH}")
    page.fill("input[name=user]", "jane@example.edu")
    page.fill("input[name=password]", "pw")
    page.click("form.stack button[type=submit]")
    page.wait_for_url(f"{live_app}/browse**")
    fake_dspace.calls.clear()
    return page


@pytest.fixture
def pdf_file(tmp_path: Path, pdf_bytes: bytes) -> Path:
    path = tmp_path / "thesis.pdf"
    path.write_bytes(pdf_bytes)
    return path
