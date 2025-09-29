"""Configuration loading: instances.yaml and mappings/*.yaml.

Parsed once at startup into Pydantic models. Every failure mode -- missing file,
malformed YAML, unknown extracted-field key, bad metadata field name -- raises
ConfigError with the offending path and a message a human can act on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Neutral field names the extractor emits (DESIGN.md §5). Mapping profiles may map
# any subset of these; anything else is a typo and fails fast.
# Phase 3's ExtractedMetadata model must stay in sync with this tuple.
EXTRACTED_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "authors",
        "advisor",
        "committee_members",
        "keywords",
        "abstract",
        "date_issued",
        "degree",
        "department",
        "language",
        "doc_type",
    }
)

# schema.element or schema.element.qualifier
_METADATA_FIELD_RE = re.compile(r"^[a-zA-Z][\w]*\.[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)?$")


class ConfigError(Exception):
    """Configuration is missing or malformed. Raised at startup; not recoverable."""

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = str(path)
        super().__init__(f"{self.path}: {message}")


class Instance(BaseModel):
    """One DSpace repository ScholarDrop can submit to."""

    model_config = ConfigDict(extra="forbid")

    key: str = ""  # filled in from the mapping key by load_instances()
    name: str
    base_url: str
    mapping_profile: str
    contact_email: str | None = None
    # Where a *person* reads this repository, as opposed to where the API lives.
    # They are the same host in production (`https://shareok.org/server` ->
    # `https://shareok.org`) and different ones in development, where the Angular
    # UI is on another port entirely -- so it is derived by default and
    # overridable, rather than assumed. Only used to link to an item a duplicate
    # warning names (DESIGN.md §12); nothing depends on it being reachable.
    ui_url: str | None = None
    # Where this repository's checksum index lives, if not beside the others.
    # Normally left unset: the path is derived from
    # SCHOLARDROP_DEDUP_CHECKSUM_INDEX_PATH plus this instance's key, because one
    # index describes exactly one repository (DESIGN.md §12). Set it for a
    # repository whose index belongs on another disk.
    dedup_checksum_index_path: str | None = None
    # Which tier 1 backend this repository's documents may go to (DESIGN.md §5):
    # cloud (Claude), local (a model on this box, zero egress), or heuristic (no
    # model at all). `llm` is the pre-3b spelling of `cloud` and still parses.
    extraction: Literal["cloud", "local", "heuristic"] = "cloud"

    @field_validator("extraction", mode="before")
    @classmethod
    def _accept_legacy_llm(cls, v: object) -> object:
        return "cloud" if v == "llm" else v

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("ui_url")
    @classmethod
    def _check_ui_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError("ui_url must start with http:// or https://")
        return v.rstrip("/")

    @property
    def api_url(self) -> str:
        """REST API root -- every DSpace endpoint hangs off this."""
        return f"{self.base_url}/api"

    @property
    def web_url(self) -> str:
        """Where a browser reads this repository.

        Defaults to `base_url` with DSpace's `/server` suffix removed, which is
        right wherever the UI and the API share a host and harmless where they do
        not (the link is a convenience beside a title that is already legible).
        """
        if self.ui_url:
            return self.ui_url
        base = self.base_url
        return base[: -len("/server")] if base.endswith("/server") else base

    def item_url(self, *, uuid: str = "", handle: str = "") -> str:
        """A human-readable link to an item.

        The uuid route is preferred: DSpace 7+ serves `/items/{uuid}` directly,
        while `/handle/{prefix}/{suffix}` goes through a redirect -- and an item
        that is only a draft has no handle at all.
        """
        if uuid:
            return f"{self.web_url}/items/{uuid}"
        if handle:
            return f"{self.web_url}/handle/{handle}"
        return ""


class MappingProfile(BaseModel):
    """Extracted field name -> DSpace metadata field, for one instance flavour."""

    model_config = ConfigDict(extra="forbid")

    name: str
    fields: dict[str, str]

    @field_validator("fields")
    @classmethod
    def _check_fields(cls, v: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(v) - EXTRACTED_FIELDS)
        if unknown:
            known = ", ".join(sorted(EXTRACTED_FIELDS))
            raise ValueError(f"unknown extracted field(s) {unknown}; expected one of: {known}")
        for key, metadata_field in v.items():
            if not isinstance(metadata_field, str) or not _METADATA_FIELD_RE.match(metadata_field):
                raise ValueError(
                    f"{key!r} maps to {metadata_field!r}, which is not a "
                    "schema.element[.qualifier] metadata field"
                )
        return v

    def metadata_field(self, extracted_field: str) -> str | None:
        return self.fields.get(extracted_field)


class Registry(BaseModel):
    """Everything loaded from disk: the instances and the profiles they reference."""

    instances: dict[str, Instance] = Field(default_factory=dict)
    profiles: dict[str, MappingProfile] = Field(default_factory=dict)

    def instance(self, key: str) -> Instance | None:
        return self.instances.get(key)

    def profile_for(self, instance: Instance) -> MappingProfile:
        # load_registry() guarantees the referenced profile exists.
        return self.profiles[instance.mapping_profile]


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(path, "file not found")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(path, f"invalid YAML: {exc}") from exc


def load_instances(path: Path) -> dict[str, Instance]:
    raw = _read_yaml(path)
    if not isinstance(raw, dict) or "instances" not in raw:
        raise ConfigError(path, "expected a top-level 'instances:' mapping")
    block = raw["instances"]
    if not isinstance(block, dict) or not block:
        raise ConfigError(path, "'instances' must be a non-empty mapping of key -> instance")

    instances: dict[str, Instance] = {}
    for key, body in block.items():
        if not isinstance(body, dict):
            raise ConfigError(path, f"instance {key!r} must be a mapping")
        try:
            instances[key] = Instance(key=key, **body)
        except ValidationError as exc:
            raise ConfigError(path, f"instance {key!r} is invalid: {_brief(exc)}") from exc
    return instances


def load_mapping_profile(path: Path) -> MappingProfile:
    raw = _read_yaml(path)
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(path, "expected a non-empty mapping of extracted field -> metadata field")
    try:
        return MappingProfile(name=path.stem, fields=raw)
    except ValidationError as exc:
        raise ConfigError(path, _brief(exc)) from exc


def load_registry(config_dir: Path) -> Registry:
    """Load instances.yaml plus every mapping profile it references."""
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise ConfigError(config_dir, "config directory not found")

    instances_path = config_dir / "instances.yaml"
    instances = load_instances(instances_path)

    profiles: dict[str, MappingProfile] = {}
    for instance in instances.values():
        name = instance.mapping_profile
        if name in profiles:
            continue
        profile_path = config_dir / "mappings" / f"{name}.yaml"
        if not profile_path.is_file():
            raise ConfigError(
                instances_path,
                f"instance {instance.key!r} references mapping profile {name!r}, "
                f"but {profile_path} does not exist",
            )
        profiles[name] = load_mapping_profile(profile_path)

    return Registry(instances=instances, profiles=profiles)


def _brief(exc: ValidationError) -> str:
    """One-line summary of a pydantic error, readable in a startup traceback."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
