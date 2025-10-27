from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scholardrop.app import create_app
from scholardrop.config import ConfigError
from scholardrop.settings import load_settings
from tests.conftest import LOGIN_PATH, SHIPPED_INSTANCE_KEY


@pytest.fixture
def client(shipped_config_dir: Path, tmp_path: Path) -> TestClient:
    settings = load_settings(
        env="dev",
        config_dir=shipped_config_dir,
        secret_key="test-secret",
        upload_dir=tmp_path / "uploads",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_index_lists_configured_instances(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "SHAREOK" in body
    # From the loaded registry, like /healthz: repointing an instance at a test
    # host is a config change, not a broken landing page.
    assert client.app.state.registry.instances[SHIPPED_INSTANCE_KEY].base_url in body
    # Percent-encoded, not raw: the shipped keys carry spaces, and a href with a
    # literal space in it is not a URL. See test_the_rendered_sign_in_link_works.
    assert LOGIN_PATH in body
    assert f"/login/{SHIPPED_INSTANCE_KEY}" not in body


def test_the_rendered_sign_in_link_works(client: TestClient) -> None:
    """Every href the landing page offers has to be a URL you can follow.

    Asserting on the markup alone missed this: an instance key is free-form
    YAML, and the shipped ones carry spaces, so the template used to emit
    `href="/login/shareok 7.3 Test"`. Browsers escape that on navigation and the
    flow appeared to work -- until a key held a `?` or a `#`, which a browser
    reads as the start of a query or a fragment and drops from the path.
    """
    import re

    hrefs = re.findall(r'href="(/login/[^"]*)"', client.get("/").text)

    assert hrefs, "the landing page offered no sign-in links at all"
    for href in hrefs:
        assert " " not in href
        assert client.get(href).status_code == 200, href


def test_healthz_reports_loaded_config(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    # Derived from the loaded registry, not a frozen list: healthz's job is to
    # report whatever config/instances.yaml holds, so adding an instance there
    # must not fail this test.
    expected = sorted(client.app.state.registry.instances)
    assert response.json() == {"status": "ok", "instances": expected, "sessions": 0}
    assert SHIPPED_INSTANCE_KEY in expected


def test_static_assets_are_served(client: TestClient) -> None:
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_bad_config_fails_at_startup(tmp_path: Path) -> None:
    settings = load_settings(env="dev", config_dir=tmp_path / "missing", secret_key="x")
    with pytest.raises(ConfigError):
        create_app(settings)
