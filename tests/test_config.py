from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scholardrop.config import (
    ConfigError,
    Instance,
    load_mapping_profile,
    load_registry,
)
from tests.conftest import VALID_INSTANCES, VALID_PROFILE


def test_loads_shipped_config(shipped_config_dir: Path) -> None:
    registry = load_registry(shipped_config_dir)

    assert "shareok" in registry.instances
    shareok = registry.instances["shareok"]
    assert shareok.name == "SHAREOK"
    assert shareok.key == "shareok"
    # Not pinned to today's values: which tier SHAREOK is configured for, and
    # which host it points at, are operational choices that change without this
    # being a regression. What must hold is that they parse and compose.
    assert shareok.extraction in {"cloud", "local", "heuristic"}
    assert shareok.api_url == f"{shareok.base_url}/api"

    profile = registry.profile_for(shareok)
    assert profile.name == "shareok-etd"
    assert profile.metadata_field("committee_members") == "dc.contributor.committeeMember"
    assert profile.metadata_field("degree") is None


def test_trailing_slash_stripped_from_base_url(write_config) -> None:
    registry = load_registry(write_config(VALID_INSTANCES, {"shareok-etd": VALID_PROFILE}))
    assert registry.instances["shareok"].base_url == "https://shareok.org/server"


def test_missing_config_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config directory not found"):
        load_registry(tmp_path / "nope")


def test_missing_instances_file(config_dir: Path) -> None:
    with pytest.raises(ConfigError, match="file not found"):
        load_registry(config_dir)


def test_malformed_yaml(write_config) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_registry(write_config("instances:\n  shareok: [unclosed\n"))


def test_missing_instances_key(write_config) -> None:
    with pytest.raises(ConfigError, match="top-level 'instances:' mapping"):
        load_registry(write_config("repositories:\n  shareok: {}\n"))


def test_empty_instances_block(write_config) -> None:
    with pytest.raises(ConfigError, match="non-empty mapping"):
        load_registry(write_config("instances: {}\n"))


def test_instance_missing_required_field(write_config) -> None:
    bad = """
instances:
  shareok:
    name: SHAREOK
    mapping_profile: shareok-etd
"""
    with pytest.raises(ConfigError, match="base_url"):
        load_registry(write_config(bad, {"shareok-etd": VALID_PROFILE}))


def test_instance_rejects_unknown_field(write_config) -> None:
    bad = VALID_INSTANCES + "    colour: purple\n"
    with pytest.raises(ConfigError, match="colour"):
        load_registry(write_config(bad, {"shareok-etd": VALID_PROFILE}))


def test_instance_rejects_non_http_base_url(write_config) -> None:
    bad = VALID_INSTANCES.replace("https://shareok.org/server/", "shareok.org")
    with pytest.raises(ConfigError, match="http"):
        load_registry(write_config(bad, {"shareok-etd": VALID_PROFILE}))


def test_instance_rejects_unknown_extraction_tier(write_config) -> None:
    bad = VALID_INSTANCES + "    extraction: telepathy\n"
    with pytest.raises(ConfigError, match="extraction"):
        load_registry(write_config(bad, {"shareok-etd": VALID_PROFILE}))


def test_missing_mapping_profile_is_reported_against_the_instance(write_config) -> None:
    with pytest.raises(ConfigError, match="references mapping profile 'shareok-etd'"):
        load_registry(write_config(VALID_INSTANCES))


def test_profile_rejects_unknown_extracted_field(config_dir: Path) -> None:
    path = config_dir / "mappings" / "bad.yaml"
    path.write_text("titel: dc.title\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown extracted field"):
        load_mapping_profile(path)


def test_profile_rejects_malformed_metadata_field(config_dir: Path) -> None:
    path = config_dir / "mappings" / "bad.yaml"
    path.write_text("title: title\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="schema.element"):
        load_mapping_profile(path)


def test_profile_rejects_empty_file(config_dir: Path) -> None:
    path = config_dir / "mappings" / "empty.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty mapping"):
        load_mapping_profile(path)


def test_shared_profile_loaded_once(write_config) -> None:
    two = """
instances:
  shareok:
    name: SHAREOK
    base_url: https://shareok.org/server
    mapping_profile: shareok-etd
  other:
    name: Other
    base_url: https://other.example.org/server
    mapping_profile: shareok-etd
"""
    registry = load_registry(write_config(two, {"shareok-etd": VALID_PROFILE}))
    assert len(registry.instances) == 2
    assert len(registry.profiles) == 1
    assert registry.profile_for(registry.instances["other"]).name == "shareok-etd"


# ---- where a person reads the repository (DESIGN.md §12) --------------------


def test_the_web_url_is_the_base_url_without_dspaces_server_suffix() -> None:
    instance = Instance(
        key="x", name="X", base_url="https://shareok.org/server", mapping_profile="p"
    )
    assert instance.web_url == "https://shareok.org"
    assert instance.item_url(uuid="abc") == "https://shareok.org/items/abc"


def test_a_ui_on_another_host_can_be_configured() -> None:
    """True of every development box, where the API is on 8080 and the Angular UI
    is on 4000 -- deriving it there would produce a link to the JSON."""
    instance = Instance(
        key="x",
        name="X",
        base_url="http://127.0.0.1:8080/server",
        mapping_profile="p",
        ui_url="http://127.0.0.1:4000/",
    )
    assert instance.web_url == "http://127.0.0.1:4000"


def test_an_item_with_no_uuid_falls_back_to_its_handle() -> None:
    instance = Instance(
        key="x", name="X", base_url="https://shareok.org/server", mapping_profile="p"
    )
    assert instance.item_url(handle="11244/1") == "https://shareok.org/handle/11244/1"
    assert instance.item_url() == ""


def test_a_ui_url_must_be_a_url() -> None:
    with pytest.raises(ValidationError):
        Instance(
            key="x",
            name="X",
            base_url="https://x.test/server",
            mapping_profile="p",
            ui_url="x.test",
        )
