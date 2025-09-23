"""The rule that decides whether a draft gets deleted (scripts/prune_drafts.py).

Every other script in this repository reports; this one destroys. `inspect()` is
the whole safety property, so it is tested the way a deletion rule should be:
mostly with things that must *survive*.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests.fakes import make_workspaceitem, metadata_values

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prune_drafts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("prune_drafts", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves annotations through it
    spec.loader.exec_module(module)
    return module


prune = load_module()


def draft(**kwargs):
    """A draft with nothing in it: no metadata, no file, licence not granted."""
    kwargs.setdefault("errors", [])
    return make_workspaceitem("7", **kwargs)


# ---- what must be deleted --------------------------------------------------


def test_a_draft_with_nothing_in_it_is_empty() -> None:
    assert prune.inspect(draft()).empty


def test_the_required_field_complaints_do_not_count_as_content() -> None:
    """An untouched draft always fails validation; that is not a person's work."""
    item = make_workspaceitem(
        "7",
        errors=[
            {
                "message": "error.validation.required",
                "paths": ["/sections/traditionalpageone/dc.title"],
            }
        ],
    )

    assert prune.inspect(item).empty


# ---- what must survive -----------------------------------------------------


def test_one_metadata_value_saves_a_draft() -> None:
    item = draft(sections={"traditionalpageone": {"dc.title": metadata_values("A start")}})

    verdict = prune.inspect(item)
    assert not verdict.empty
    assert "dc.title" in verdict.reason


def test_a_value_in_any_section_saves_a_draft() -> None:
    """Not just page one: a collection's form can put fields anywhere."""
    item = draft(sections={"traditionalpagetwo": {"dc.subject": metadata_values("soils")}})

    assert not prune.inspect(item).empty


def test_a_file_saves_a_draft() -> None:
    item = draft(files=[{"name": "thesis.pdf"}])

    verdict = prune.inspect(item)
    assert not verdict.empty
    assert verdict.reason == "has a file"


def test_a_granted_licence_saves_a_draft() -> None:
    """Nothing typed, but somebody read something and agreed to it."""
    item = draft(sections={"license": {"granted": True, "url": None}})

    assert not prune.inspect(item).empty


def test_an_unfamiliar_validation_error_saves_a_draft() -> None:
    """Something happened here that this script does not understand."""
    item = make_workspaceitem(
        "7", errors=[{"message": "error.validation.custom.embargo", "paths": ["/sections/x"]}]
    )

    assert not prune.inspect(item).empty


def test_a_draft_with_no_readable_sections_is_never_deleted() -> None:
    """Unable to tell is a reason to skip, not a reason to guess."""
    from scholardrop.dspace.models import WorkspaceItem

    blind = WorkspaceItem(id="7", sections={})

    verdict = prune.inspect(blind)
    assert not verdict.empty
    assert "judge" in verdict.reason


def test_an_empty_string_value_is_not_content() -> None:
    """DSpace keeps a row for a field the user cleared; it holds nothing."""
    item = draft(sections={"traditionalpageone": {"dc.title": [{"value": "  "}]}})

    assert prune.inspect(item).empty


# ---- age -------------------------------------------------------------------


def test_a_draft_changed_moments_ago_is_not_old_enough_to_touch() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    item = make_workspaceitem("7", errors=[])
    item.last_modified = now

    assert (prune.age_days(item) or 0) < 1.0


def test_an_unreadable_timestamp_reports_no_age() -> None:
    item = make_workspaceitem("7", errors=[])
    item.last_modified = "whenever"

    assert prune.age_days(item) is None
