from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scholardrop.app import create_app
from scholardrop.config import ConfigError
from scholardrop.settings import load_settings


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
    assert "https://shareok.org/server" in body
    assert "/login/shareok" in body


def test_healthz_reports_loaded_config(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    # Derived from the loaded registry, not a frozen list: healthz's job is to
    # report whatever config/instances.yaml holds, so adding an instance there
    # must not fail this test.
    expected = sorted(client.app.state.registry.instances)
    assert response.json() == {"status": "ok", "instances": expected, "sessions": 0}
    assert "shareok" in expected


def test_static_assets_are_served(client: TestClient) -> None:
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_bad_config_fails_at_startup(tmp_path: Path) -> None:
    settings = load_settings(env="dev", config_dir=tmp_path / "missing", secret_key="x")
    with pytest.raises(ConfigError):
        create_app(settings)
