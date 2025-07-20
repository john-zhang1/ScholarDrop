"""The eval harness's scoring (scripts/eval_extract.py).

The other scripts are thin drivers and are exercised by hand; this one is not.
Its numbers are what the Phase 3b gate decides on -- "is the local model close
enough to Claude" -- and a scorer that is quietly wrong is worse than no harness,
because it looks like evidence.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_extract.py"


def load_module():
    """`scripts/` is not a package, so load the file directly."""
    spec = importlib.util.spec_from_file_location("eval_extract", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules and raises on a module that is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = load_module()


def test_identical_values_score_one() -> None:
    assert ev.score_scalar("A Study of Prairie Soils", "A Study of Prairie Soils") == 1.0


def test_typography_is_not_a_difference() -> None:
    """PDF text is full of odd spacing and dashes; that is not a wrong answer."""
    assert (
        ev.score_scalar("Soils  of\nthe   Plains—A Survey.", "Soils of the Plains-A Survey") == 1.0
    )


def test_a_different_answer_scores_zero() -> None:
    assert ev.score_scalar("Prairie Soils", "Marine Sediment Cores of the Gulf") == 0.0


def test_name_order_is_not_a_miss() -> None:
    """Tier 1 keeps the document's order; the golden may use the other one."""
    assert ev.score_scalar("Okonkwo-Bell, Margaret", "Margaret Okonkwo-Bell", names=True) == 1.0
    # Only for people: two titles sharing words are not the same title.
    assert ev.score_scalar("Soils of Kansas", "Kansas of Soils") < 1.0


def test_an_absent_field_is_right_only_when_nothing_was_returned() -> None:
    """Inventing a value the document does not have is the failure that matters."""
    assert ev.score_scalar(None, None) == 1.0
    assert ev.score_scalar("Dr. Someone", None) == 0.0
    assert ev.score_list([], []) == 1.0
    assert ev.score_list(["Invented, Person"], []) == 0.0


def test_lists_score_by_f1_so_extras_and_misses_both_count() -> None:
    want = ["Adeyemi, Samuel", "Raghunathan, Priya"]
    assert ev.score_list(list(want), want) == 1.0
    half = ev.score_list(["Adeyemi, Samuel"], want)  # one of two: recall 0.5
    assert 0.5 < half < 1.0
    padded = ev.score_list([*want, "Nobody, A."], want)  # an extra name costs
    assert padded < 1.0
    assert ev.score_list(["Someone Else"], want) == 0.0


def test_gate_fields_are_the_ones_the_phase_is_about() -> None:
    assert set(ev.GATE_FIELDS) == {
        "title",
        "authors",
        "abstract",
        "advisor",
        "committee_members",
    }


def test_backend_availability_reflects_configuration() -> None:
    from scholardrop.settings import load_settings

    bare = load_settings(secret_key="x", anthropic_api_key="", local_llm_model="")
    assert set(ev.available(bare)) == {"heuristic"}

    both = load_settings(secret_key="x", anthropic_api_key="k")
    assert set(ev.available(both)) == {"heuristic", "cloud", "local"}


@pytest.mark.parametrize("field_name", ["title", "authors", "abstract"])
def test_score_only_counts_fields_the_golden_asserts(field_name: str) -> None:
    from scholardrop.extract.models import ExtractedMetadata

    scores = ev.score(ExtractedMetadata(), {field_name: None})
    assert list(scores) == [field_name]
