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
    # Tier 1 gets one more go at a *retryable* failure (429, 5xx, connection drop,
    # a cold grammar compile timing out) before the submission is degraded to
    # heuristics for the rest of its life. Nothing here retries a refusal, a bad
    # key, or a malformed request -- those fail the same way twice.
    extraction_retry_attempts: int = 2
    extraction_retry_backoff_seconds: float = 2.0
    # The first parse call with a given output schema pays for compiling its
    # grammar server-side, which is slow enough to time out; a throwaway call at
    # startup means no real submission is the one that pays. Costs one small
    # request per process start.
    extraction_warm_on_startup: bool = True

    # Tier 1, local backend (DESIGN.md §5, "Tier 1 backends and the embargo
    # case"). This is where an embargoed document's text goes: a model on this
    # box, bound to loopback, so nothing leaves the machine. Blank either of the
    # first two and the local backend is unconfigured -- a submission marked
    # restricted then falls back to heuristics rather than to the cloud.
    local_llm_base_url: str = "http://127.0.0.1:11434"
    local_llm_model: str = "qwen3:4b-instruct"
    # ollama -> {base}/api/chat with the schema in `format`; openai ->
    # {base}/v1/chat/completions with response_format.json_schema (vLLM,
    # llama.cpp server, LM Studio).
    local_llm_dialect: Literal["ollama", "openai"] = "ollama"
    # A Q4 4B model on a CPU-only box takes 30-90s for a typical ETD, and the
    # first call also loads the weights. Long, because the alternative to waiting
    # is silently degrading an embargoed submission to heuristics.
    local_llm_timeout_seconds: float = 300.0
    # Ollama's default context is 4k and it drops the *tail* of a longer prompt --
    # which is exactly where an ETD's signature page is. Must comfortably hold
    # local_llm_max_chars (~4 chars/token) plus the answer.
    local_llm_num_ctx: int = 8192
    # Input budget for the local backend, spent deliberately across title page,
    # abstract, and signature tail (llm._budgeted_text) rather than head-first.
    local_llm_max_chars: int = 12_000
    # Load the local weights at startup when an instance is configured for it,
    # for the same reason the cloud grammar is warmed: no submission should be
    # the request that pays a one-off cost.
    local_llm_warm_on_startup: bool = True

    # Duplicate detection (DESIGN.md §12). Both layers are advisory and both are
    # allowed to be off: a repository with no harvest behind it, or one that would
    # rather not spend a Solr query per review page, still submits normally.
    dedup_enabled: bool = True
    # Layer 1: the exact-file check. Needs an index built by
    # scripts/harvest_checksums.py; without the file the check is quietly skipped.
    dedup_checksum_enabled: bool = True
    dedup_checksum_index_path: Path = _REPO_ROOT / "var" / "checksums.sqlite"
    # Layer 2's *fallback* only -- the repository's own duplicates section, when it
    # has one, is free and always read. This flag governs the title query
    # ScholarDrop runs itself when the section is absent.
    dedup_title_query_enabled: bool = True
    # How alike two titles must be to be shown as the same work. High on purpose:
    # a panel that cries wolf is a panel nobody reads (dedup/titles.py).
    dedup_title_threshold: float = 0.90
    dedup_max_matches: int = 5

    # Identifier-first submission (DESIGN.md §13.1). Off switches the identifier
    # box off entirely; a repository that would rather every deposit began with a
    # file can have that.
    identifiers_enabled: bool = True
    # Prefer the repository's own live-import providers (crossref, arxiv,
    # pubmed...) so the *server* maps the record with its own configuration.
    # Falls back to registry_resolution_enabled when the instance has no provider
    # for the identifier, or its provider fails.
    identifiers_native_source_enabled: bool = True
    # ScholarDrop asking the registries itself. Identifiers are not document
    # content, so this is allowed even for a submission marked restricted -- but
    # an instance that must make no outbound calls at all can switch it off.
    registry_resolution_enabled: bool = True
    # Crossref's polite pool wants a contact address; without one the calls still
    # work, from the anonymous pool.
    registry_mailto: str = ""
    # Registries are quick when they are up, and this runs inside the extraction
    # job the browser is already polling -- so short enough not to hold a
    # submission, long enough to survive a slow first byte.
    registry_timeout_seconds: float = 20.0

    # ORCID author lookup (DESIGN.md §13.2). Assistance on the review form, never
    # a gate: manual entry always works, and an unreachable ORCID is silent.
    orcid_enabled: bool = True
    orcid_api_url: str = "https://pub.orcid.org/v3.0"
    # Short. This runs while somebody is typing a name, and a slow answer to a
    # query they have already moved on from is worth nothing.
    orcid_timeout_seconds: float = 8.0
    orcid_max_results: int = 5
    # Below this many characters the type-ahead does not ask. "Sm" matches tens of
    # thousands of people and tells the person typing nothing.
    orcid_min_query_length: int = 3

    # The submission status page (DESIGN.md §13.3). Off means no deposit is
    # logged and /my-submissions lists only what DSpace itself knows -- the
    # drafts. Worth having: the log is the one piece of state this app keeps
    # about a user beyond their session.
    submissions_log_enabled: bool = True
    submissions_log_path: Path = _REPO_ROOT / "var" / "submissions.sqlite"
    # How many rows the status page shows. Not pagination: the page merges two
    # sources with independent orderings, which cannot be paged consistently
    # without merging the whole of both anyway, and one person's own submission
    # history is small. Each logged row costs a live lookup, so this is also the
    # bound on how many requests the page makes.
    submissions_max_rows: int = 25
    # Live lookups run concurrently against one httpx client. Enough to keep the
    # page quick, low enough not to be a small load test aimed at the repository
    # every time somebody opens it.
    submissions_lookup_concurrency: int = 6

    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 300

    # How long the cached repository configuration (submission definitions, forms,
    # vocabularies, licences, collection names) stays good. Long, because changing
    # any of it means reconfiguring and restarting DSpace -- but not forever, or a
    # reconfigured collection serves a stale form until someone restarts this app.
    config_cache_ttl_seconds: int = 3600

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
