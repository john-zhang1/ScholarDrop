"""Process settings, from the environment (SCHOLARDROP_* / .env).

Distinct from config.py: this is deployment configuration (where things live, how
long sessions last), not repository configuration.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCHOLARDROP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "production"] = "dev"
    config_dir: Path = _REPO_ROOT / "config"

    # Signs the session cookie. Required in production; a per-process random key
    # is generated in dev, which means restarting the app logs everyone out.
    secret_key: str = ""

    session_cookie_name: str = "scholardrop_session"
    session_cookie_secure: bool = True
    # 30 min matches the DSpace JWT lifetime (DESIGN.md §3) -- no point holding a
    # session longer than the token it carries.
    session_ttl_seconds: int = 1800
    session_sweep_interval_seconds: int = 300

    upload_dir: Path = _REPO_ROOT / "uploads"
    upload_max_bytes: int = 100 * 1024 * 1024  # 100 MB; ETDs with images get large
    # How long an untouched upload directory survives. Longer than a session, so a
    # user who signs back in mid-review still has their file; short enough that an
    # abandoned thesis is not sitting on disk a day later.
    upload_ttl_seconds: int = 7200

    # Extraction tier 1 (DESIGN.md §5). Key may be absent -- the extractor falls
    # back to heuristics rather than failing the flow.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # Extraction is a read-and-report task, so low effort is fast and sufficient;
    # raise it if the gate run shows fields being missed. Thinking is left at the
    # model default (adaptive) rather than disabled.
    anthropic_effort: Literal["low", "medium", "high"] = "low"

    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 300

    log_level: str = "INFO"

    _generated_secret: bool = False

    @model_validator(mode="after")
    def _resolve_secret_key(self) -> Settings:
        if not self.secret_key:
            if self.env == "production":
                raise ValueError(
                    "SCHOLARDROP_SECRET_KEY must be set when SCHOLARDROP_ENV=production"
                )
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(32))
            object.__setattr__(self, "_generated_secret", True)
        return self

    @property
    def secret_key_was_generated(self) -> bool:
        """True when running on a throwaway dev key (sessions die on restart)."""
        return self._generated_secret


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
