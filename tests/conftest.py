from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    )
    return create_app(
        settings,
        client_factory=lambda instance: fake_dspace,
        extractor=fake_extractor,
    )


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
    response = web.post("/login/shareok", data={"user": "jane@example.edu", "password": "pw"})
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
