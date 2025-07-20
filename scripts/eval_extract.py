#!/usr/bin/env python
"""Compare extraction backends against golden metadata (IMPLEMENTATION_PLAN 3b).

    .venv/bin/python scripts/eval_extract.py                    # every backend available
    .venv/bin/python scripts/eval_extract.py --backend local heuristic
    .venv/bin/python scripts/eval_extract.py --dir /tmp/etds --verbose

Fixtures live in `tests/fixtures/etds/` (gitignored -- real ETDs are not committed):

    thesis.pdf          the document, or thesis.txt for front matter already extracted
    thesis.json         the golden metadata, in the extractor's neutral field names

A golden file only has to name the fields it asserts; anything absent is skipped
rather than counted as a miss, so a partial golden is still useful. Write one by
running `scripts/extract_cli.py doc.pdf --json`, fixing what is wrong by hand, and
keeping only the fields you checked against the document.

The point of the table is not a leaderboard. It is the Phase 3b gate question:
*is the local model close enough to Claude on title/authors/abstract/committee
that an embargoed submission is not a materially worse submission?*
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.config import EXTRACTED_FIELDS  # noqa: E402
from scholardrop.extract import heuristic, llm, read_pdf  # noqa: E402
from scholardrop.extract.backends import (  # noqa: E402
    AnthropicBackend,
    LlmUnavailable,
    local_backend,
    local_is_configured,
)
from scholardrop.extract.models import ExtractedMetadata  # noqa: E402
from scholardrop.extract.reader import PdfDocument  # noqa: E402
from scholardrop.settings import Settings, load_settings  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "etds"
# The fields the gate is actually about; the rest are reported but not summarised.
GATE_FIELDS = ("title", "authors", "abstract", "advisor", "committee_members")
# Fields whose values are people, compared without caring which way round the
# document printed the name.
NAME_FIELDS = frozenset({"authors", "advisor", "committee_members"})
# Below this, two strings are different answers rather than two spellings of one.
NEAR = 0.95
PARTIAL = 0.70


# --- scoring -------------------------------------------------------------


def normalise(value: str) -> str:
    """Compare meaning, not typography: PDFs are full of odd spacing and dashes."""
    text = value.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip(" .,;: ")
    return text.casefold()


def name_key(value: str) -> str:
    """A name reduced to its words, order-insensitively.

    Tier 1 is told to keep the document's own order, so the same person is
    "Okonkwo-Bell, Margaret" on the signature page and "Margaret Okonkwo-Bell" in
    the golden file. Scoring that as a miss would send someone off to fix a prompt
    that is doing exactly what it was told.
    """
    return " ".join(sorted(t for t in re.split(r"[^\w']+", normalise(value)) if t))


def similarity(left: str, right: str, *, names: bool = False) -> float:
    if names and name_key(left) == name_key(right):
        return 1.0
    return difflib.SequenceMatcher(None, normalise(left), normalise(right)).ratio()


def score_scalar(got: str | None, want: str | None, *, names: bool = False) -> float:
    if not want:
        # The golden says this field is absent. Saying nothing is right; inventing
        # something is the failure Phase 3's prompt rules exist to prevent.
        return 1.0 if not got else 0.0
    if not got:
        return 0.0
    ratio = similarity(got, want, names=names)
    if ratio >= NEAR:
        return 1.0
    return 0.5 if ratio >= PARTIAL else 0.0


def score_list(got: list[str], want: list[str], *, names: bool = False) -> float:
    """F1 over the values, with near-matches counted as hits."""
    if not want:
        return 1.0 if not got else 0.0
    if not got:
        return 0.0
    remaining = list(want)
    hits = 0.0
    for value in got:
        best = max(((similarity(value, w, names=names), w) for w in remaining), default=(0.0, None))
        if best[0] >= PARTIAL and best[1] is not None:
            hits += 1.0 if best[0] >= NEAR else 0.5
            remaining.remove(best[1])
    precision = hits / len(got)
    recall = hits / len(want)
    return 0.0 if not hits else 2 * precision * recall / (precision + recall)


def value_of(metadata: ExtractedMetadata, name: str):
    extracted = metadata.field(name)
    return extracted.value if extracted.present else ([] if name.endswith("s") else None)


def score(metadata: ExtractedMetadata, golden: dict) -> dict[str, float]:
    """One score per asserted field: 1.0 right, 0.5 close, 0.0 wrong."""
    scores: dict[str, float] = {}
    for name, want in golden.items():
        if name not in EXTRACTED_FIELDS:
            continue
        got = value_of(metadata, name)
        names = name in NAME_FIELDS
        if isinstance(want, list) or isinstance(got, list):
            scores[name] = score_list(
                list(got or []),
                list(want or []) if isinstance(want, list) else [want],
                names=names,
            )
        else:
            scores[name] = score_scalar(got, want, names=names)
    return scores


# --- running -------------------------------------------------------------


@dataclass
class Fixture:
    name: str
    path: Path
    golden: dict

    def document(self) -> PdfDocument:
        if self.path.suffix.lower() == ".pdf":
            return read_pdf(self.path)
        text = self.path.read_text(encoding="utf-8", errors="replace")
        return PdfDocument(page_count=0, front_text=text)


@dataclass
class Run:
    fixture: str
    scores: dict[str, float]
    seconds: float
    note: str = ""


@dataclass
class BackendResult:
    name: str
    label: str
    runs: list[Run] = field(default_factory=list)

    def per_field(self) -> dict[str, float]:
        totals: dict[str, list[float]] = {}
        for run in self.runs:
            for name, value in run.scores.items():
                totals.setdefault(name, []).append(value)
        return {name: sum(v) / len(v) for name, v in totals.items()}

    def gate_score(self) -> float:
        fields = self.per_field()
        scored = [fields[name] for name in GATE_FIELDS if name in fields]
        return sum(scored) / len(scored) if scored else 0.0

    def seconds(self) -> float:
        return sum(run.seconds for run in self.runs) / len(self.runs) if self.runs else 0.0


def load_fixtures(directory: Path) -> list[Fixture]:
    fixtures = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".pdf", ".txt"}:
            continue
        golden_path = path.with_suffix(".json")
        if not golden_path.is_file():
            print(f"{YELLOW}skipping {path.name}: no {golden_path.name}{RESET}")
            continue
        fixtures.append(
            Fixture(path.stem, path, json.loads(golden_path.read_text(encoding="utf-8")))
        )
    return fixtures


async def extract_with(name: str, document: PdfDocument, settings: Settings) -> ExtractedMetadata:
    if name == "heuristic":
        return heuristic.extract(document)
    backend = AnthropicBackend(settings) if name == "cloud" else local_backend(settings)
    return await llm.extract(document, settings, backend=backend)


def available(settings: Settings) -> dict[str, str]:
    """Which backends this box can actually run, and what to call them."""
    backends = {"heuristic": "tier 2 (no model)"}
    if settings.anthropic_api_key:
        backends["cloud"] = f"anthropic/{settings.anthropic_model}"
    if local_is_configured(settings):
        backends["local"] = f"{settings.local_llm_dialect}/{settings.local_llm_model}"
    return backends


async def run(
    names: list[str], fixtures: list[Fixture], settings: Settings, *, verbose: bool
) -> list[BackendResult]:
    labels = available(settings)
    results = []
    for name in names:
        result = BackendResult(name, labels.get(name, name))
        for fixture in fixtures:
            document = fixture.document()
            started = time.monotonic()
            try:
                metadata = await extract_with(name, document, settings)
                note = ""
            except LlmUnavailable as exc:
                # Recorded as a zero rather than skipped: a backend that is not
                # there is a backend that extracts nothing.
                metadata = ExtractedMetadata()
                note = str(exc)
            elapsed = time.monotonic() - started
            scores = score(metadata, fixture.golden)
            result.runs.append(Run(fixture.name, scores, elapsed, note))
            print(
                f"{DIM}  {name:<10} {fixture.name:<28} "
                f"{sum(scores.values()) / max(len(scores), 1):.2f}  {elapsed:5.1f}s"
                f"{('  ' + note) if note else ''}{RESET}"
            )
            if verbose:
                report_diff(metadata, fixture)
        results.append(result)
    return results


def report_diff(metadata: ExtractedMetadata, fixture: Fixture) -> None:
    for name, want in fixture.golden.items():
        if name not in EXTRACTED_FIELDS:
            continue
        got = value_of(metadata, name)
        if score(metadata, {name: want}).get(name, 0.0) >= 1.0:
            continue
        print(f"      {name}\n        want: {want!r}\n        got:  {got!r}")


# --- reporting -----------------------------------------------------------


def cell(value: float) -> str:
    colour = GREEN if value >= 0.9 else YELLOW if value >= 0.6 else RED
    return f"{colour}{value:>5.2f}{RESET}"


def table(results: list[BackendResult]) -> None:
    if not results:
        return
    fields = sorted({name for r in results for name in r.per_field()})
    width = max((len(f) for f in fields), default=10) + 2

    print(f"\n{BOLD}Per-field accuracy{RESET} (1.00 = matches the golden file)\n")
    print(f"{'field':<{width}}" + "".join(f"{r.name:>10}" for r in results))
    for name in fields:
        marker = "*" if name in GATE_FIELDS else " "
        row = "".join(
            f"{cell(r.per_field().get(name, 0.0)):>19}" if name in r.per_field() else f"{'—':>10}"
            for r in results
        )
        print(f"{marker}{name:<{width - 1}}" + row)

    print(f"\n{'gate fields *':<{width}}" + "".join(f"{cell(r.gate_score()):>19}" for r in results))
    print(f"{'seconds/doc':<{width}}" + "".join(f"{r.seconds():>10.1f}" for r in results))
    print(f"\n{DIM}" + "  ".join(f"{r.name}={r.label}" for r in results) + f"{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", type=Path, default=FIXTURE_DIR, help="fixture directory")
    parser.add_argument(
        "--backend", nargs="+", help="cloud, local, heuristic (default: all available)"
    )
    parser.add_argument("--verbose", action="store_true", help="show every field that missed")
    args = parser.parse_args()

    settings = load_settings(secret_key="eval")
    backends = available(settings)
    names = args.backend or list(backends)
    unknown = [n for n in names if n not in {"cloud", "local", "heuristic"}]
    if unknown:
        print(f"{RED}unknown backend(s): {', '.join(unknown)}{RESET}")
        return 2
    for name in names:
        if name not in backends:
            env = "SCHOLARDROP_ANTHROPIC_API_KEY" if name == "cloud" else "SCHOLARDROP_LOCAL_LLM_*"
            print(f"{YELLOW}{name} is not configured here ({env}); it will score 0{RESET}")

    if not args.dir.is_dir():
        print(f"{RED}no fixture directory at {args.dir}{RESET}")
        print(f"{DIM}put doc.pdf + doc.json pairs there; see this script's docstring{RESET}")
        return 2
    fixtures = load_fixtures(args.dir)
    if not fixtures:
        print(f"{RED}no fixtures with golden files in {args.dir}{RESET}")
        return 2

    print(f"{BOLD}{len(fixtures)} fixture(s) x {len(names)} backend(s){RESET}")
    results = asyncio.run(run(names, fixtures, settings, verbose=args.verbose))
    table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
