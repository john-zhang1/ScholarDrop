from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The instance in the *shipped* config that route tests sign in to. The `app`
# fixture loads `config/` rather than a fixture file -- deliberately, so the
# config a deployment actually runs is the one the suite exercises -- which means
# renaming an instance there breaks every test that logs in. Naming the key once
# is what keeps that a one-line fix instead of a sweep through fifty call sites.
#
# Its base_url is `https://test.shareok.org/server`, which is what
# tests/test_routes_submissions.py's BASE has to stay equal to: the submissions
# log filters on base_url *and* instance_key, so both halves have to match the
# session a test signed in with.
SHIPPED_INSTANCE_KEY = "shareok 7.3 Test"

# Any key in the shipped config works here *except* `local`, which
# tests/test_routes_dedup.py uses as the deliberately-different second instance
# when proving that one repository's checksum index is never read for another.

# Pre-encoded, because the shipped keys contain spaces. httpx leaves existing
# percent-escapes alone, and Playwright needs a valid URL rather than one the
# browser would have to guess at.
LOGIN_PATH = f"/login/{quote(SHIPPED_INSTANCE_KEY)}"


def shipped_base_url(app) -> str:
    """The base_url a test's signed-in session will actually carry.

    Read from the registry rather than copied, because the submissions log filters
    on base_url *and* eperson_uuid: a hardcoded copy keeps passing right up until
    the shipped config repoints that instance at another host, and then every row
    a test writes stops matching the session that wrote it.
    """
    return app.state.registry.instance(SHIPPED_INSTANCE_KEY).base_url


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="run the browser tests in tests/e2e (needs playwright and a system chromium)",
    )


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "e2e: drives a real browser; needs --e2e")
    config.addinivalue_line("markers", "no_js: runs with JavaScript disabled")
    config.addinivalue_line("markers", "allow_console: tolerates browser console errors")


def pytest_collection_modifyitems(config, items) -> None:
    """Split the two suites: they cannot share a process.

    Without `--e2e`, the browser tests skip, so `pytest -q` works on a box with no
    browser. With it, *only* the browser tests run -- and that is not tidiness.
    Playwright's sync API refuses to work inside a running event loop, and this
    project is `asyncio_mode = auto`, so an async test anywhere earlier in the
    session leaves one running and every browser test then dies with
    "Runner.run() cannot be called from a running event loop". Running them
    together is the failure mode; keeping them apart is the fix.
    """
    if not config.getoption("--e2e"):
        skip = pytest.mark.skip(reason="browser test; pass --e2e to run")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip)
        return

    browser_tests = [item for item in items if "e2e" in item.keywords]
    deselected = [item for item in items if "e2e" not in item.keywords]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = browser_tests


@pytest.fixture(autouse=True)
def _isolate_from_local_environment(monkeypatch):
    """Settings under test are the documented defaults, not this machine's.

    pydantic-settings reads `.env` relative to the cwd and `SCHOLARDROP_*` from the
    environment, so a developer whose `.env` sets `SESSION_COOKIE_SECURE=false` for
    local HTTP would fail the defaults test -- and, less visibly, run every other
    test against settings nobody chose.
    """
    from scholardrop.settings import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in [key for key in os.environ if key.startswith("SCHOLARDROP_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def shipped_config_dir() -> Path:
    """The config/ directory that ships with the repo."""
    return REPO_ROOT / "config"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """An empty config directory tests can populate with fixtures."""
    d = tmp_path / "config"
    (d / "mappings").mkdir(parents=True)
    return d


@pytest.fixture
def write_config(config_dir: Path):
    def _write(instances: str, profiles: dict[str, str] | None = None) -> Path:
        (config_dir / "instances.yaml").write_text(instances, encoding="utf-8")
        for name, body in (profiles or {}).items():
            (config_dir / "mappings" / f"{name}.yaml").write_text(body, encoding="utf-8")
        return config_dir

    return _write


@pytest.fixture
def fake_dspace():
    """The single FakeDSpaceClient every request in a test will be handed."""
    from tests.fakes import FakeDSpaceClient

    return FakeDSpaceClient()


@pytest.fixture
def fake_extractor():
    """The stand-in for the PDF pipeline; route tests never open a real PDF."""
    from tests.fakes import FakeExtractor

    return FakeExtractor()


@pytest.fixture
def app(shipped_config_dir: Path, tmp_path: Path, fake_dspace, fake_extractor):
    from scholardrop.app import create_app
    from scholardrop.settings import load_settings

    settings = load_settings(
        env="dev",
        config_dir=shipped_config_dir,
        secret_key="test-secret",
        upload_dir=tmp_path / "uploads",
        session_cookie_secure=False,
        # Never the repo's real one: a developer who has run the harvester would
        # otherwise be testing against whatever their DSpace happens to hold.
        dedup_checksum_index_path=tmp_path / "checksums.sqlite",
        # Same reasoning: a developer's own deposit history must not turn up in a
        # test, and a test must not append to it.
        submissions_log_path=tmp_path / "submissions.sqlite",
    )
    return create_app(
        settings,
        client_factory=lambda instance: fake_dspace,
        extractor=fake_extractor,
    )


@pytest.fixture
def checksum_index(app):
    """Build the duplicate index the app is configured to read (DESIGN.md §12).

    A factory rather than a fixture value, because the digest under test is the
    one of whatever the test uploads.
    """
    from scholardrop.dedup.index import ChecksumIndex, IndexedBitstream, index_path_for

    def _build(
        *rows: IndexedBitstream,
        instance_key: str = SHIPPED_INSTANCE_KEY,
        base_url: str | None = None,
    ):
        # Written where *that instance* will look for it, and stamped with its
        # base_url -- an index harvested from anywhere else is deliberately refused.
        instance = app.state.registry.instance(instance_key)
        path = index_path_for(app.state.settings.dedup_checksum_index_path, instance_key)
        with ChecksumIndex.create(path, base_url=base_url or instance.base_url) as index:
            index.upsert(rows)

    return _build


@pytest.fixture
def pdf_bytes() -> bytes:
    """The smallest thing that passes the magic-byte check."""
    return b"%PDF-1.7\n%\xc7\xec\x8f\xa2\ntrailer<</Root 1 0 R>>\n%%EOF\n"


@pytest.fixture
def web(app):
    """TestClient that does not auto-follow redirects, so tests can assert on them."""
    from fastapi.testclient import TestClient

    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def signed_in(app, web, fake_dspace):
    """A browser with a live session cookie, as if login had succeeded.

    Goes through the real POST /login so the cookie, session contents, and
    write-back all match production rather than being hand-assembled.
    """
    response = web.post(LOGIN_PATH, data={"user": "jane@example.edu", "password": "pw"})
    assert response.status_code == 303, response.text
    fake_dspace.calls.clear()
    return web


@pytest.fixture
def with_collection(signed_in, fake_dspace):
    """A signed-in browser that has already chosen a collection to submit to."""
    from tests.fakes import make_collection

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    assert signed_in.get("/select/c1?verified=1").status_code == 303
    fake_dspace.calls.clear()
    return signed_in


VALID_INSTANCES = """
instances:
  shareok:
    name: SHAREOK
    base_url: https://shareok.org/server/
    mapping_profile: shareok-etd
    contact_email: shareok@ou.edu
"""

VALID_PROFILE = """
title: dc.title
authors: dc.contributor.author
committee_members: dc.contributor.committeeMember
"""
