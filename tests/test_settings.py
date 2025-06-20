from __future__ import annotations

import pytest
from pydantic import ValidationError

from scholardrop.settings import load_settings


def test_dev_generates_a_secret_key() -> None:
    settings = load_settings(env="dev", secret_key="")

    assert settings.secret_key
    assert settings.secret_key_was_generated is True


def test_explicit_secret_key_is_kept() -> None:
    settings = load_settings(env="dev", secret_key="from-the-environment")

    assert settings.secret_key == "from-the-environment"
    assert settings.secret_key_was_generated is False


def test_production_requires_a_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        load_settings(env="production", secret_key="")


def test_defaults() -> None:
    settings = load_settings(secret_key="x")

    assert settings.session_ttl_seconds == 1800
    assert settings.session_cookie_secure is True
    assert settings.anthropic_model == "claude-opus-5"
