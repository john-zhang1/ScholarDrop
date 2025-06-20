#!/usr/bin/env python
"""Extract metadata from a PDF and print it, for eyeballing.

    .venv/bin/python scripts/extract_cli.py thesis.pdf
    .venv/bin/python scripts/extract_cli.py thesis.pdf --tier heuristic --json
    .venv/bin/python scripts/extract_cli.py *.pdf --mapped

Tier 1 needs SCHOLARDROP_ANTHROPIC_API_KEY; without it the run falls back to
heuristics and says so. Nothing touches DSpace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.config import load_registry  # noqa: E402
from scholardrop.extract import map_metadata, read_pdf  # noqa: E402
from scholardrop.extract.models import ExtractedMetadata, SourceTier  # noqa: E402
from scholardrop.extract.pipeline import extract_from_document  # noqa: E402
from scholardrop.settings import load_settings  # noqa: E402

DIM, BOLD, YELLOW, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[0m"


def as_json(metadata: ExtractedMetadata) -> str:
    return json.dumps(metadata.model_dump(mode="json"), indent=2, ensure_ascii=False)


def print_human(path: Path, metadata: ExtractedMetadata, document, elapsed: float) -> None:
    print(f"\n{BOLD}{path.name}{RESET}")
    notes = [f"{document.page_count} pages", f"tier: {metadata.tier_used.value}", f"{elapsed:.1f}s"]
    if document.encrypted:
        notes.append("encrypted")
    if document.probably_scanned:
        notes.append("little/no text (scanned?)")
    print(f"{DIM}{' · '.join(notes)}{RESET}")
    if metadata.fallback_reason:
        print(f"{YELLOW}fell back to heuristics: {metadata.fallback_reason}{RESET}")

    if metadata.is_empty():
        print(f"{YELLOW}  nothing extracted{RESET}")
        return

    for name in metadata.field_names():
        extracted = metadata.field(name)
        if not extracted.present:
            print(f"  {DIM}{name:<18} —{RESET}")
            continue
        value = extracted.value
        shown = "; ".join(value) if isinstance(value, list) else str(value)
        if len(shown) > 300:
            shown = shown[:297] + "..."
        flag = f" {YELLOW}[low confidence]{RESET}" if extracted.low_confidence else ""
        provenance = f"({extracted.source_tier.value}, {extracted.confidence:.2f})"
        print(f"  {name:<18} {shown}")
        print(f"  {DIM}{'':<18} {provenance}{RESET}{flag}")


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    registry = load_registry(settings.config_dir)
    instance = registry.instance(args.instance)
    if instance is None:
        raise SystemExit(
            f"no instance {args.instance!r}; known: {', '.join(sorted(registry.instances))}"
        )
    if args.tier:
        instance = instance.model_copy(update={"extraction": args.tier})
    if instance.extraction == "llm" and not settings.anthropic_api_key:
        print(
            f"{YELLOW}note: SCHOLARDROP_ANTHROPIC_API_KEY is not set, so tier 1 will "
            f"fail and heuristics will run{RESET}",
            file=sys.stderr,
        )

    problems = 0
    for raw_path in args.pdfs:
        path = Path(raw_path)
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            problems += 1
            continue
        started = time.monotonic()
        document = read_pdf(path)
        metadata = await extract_from_document(document, instance=instance, settings=settings)
        elapsed = time.monotonic() - started

        if args.json:
            print(as_json(metadata))
        else:
            print_human(path, metadata, document, elapsed)

        if args.mapped:
            mapped = map_metadata(metadata, registry.profile_for(instance))
            print(f"\n{DIM}mapped via {instance.mapping_profile}:{RESET}")
            print(json.dumps(mapped.fields, indent=2, ensure_ascii=False))
            if mapped.unmapped:
                print(f"{YELLOW}not placed by the profile: {sorted(mapped.unmapped)}{RESET}")

        if metadata.tier_used is SourceTier.NONE:
            problems += 1

    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pdfs", nargs="+", help="PDF file(s) to extract from")
    parser.add_argument("--instance", default="shareok", help="instance key (default shareok)")
    parser.add_argument(
        "--tier", choices=["llm", "heuristic"], help="override the instance's configured tier"
    )
    parser.add_argument("--json", action="store_true", help="print ExtractedMetadata as JSON")
    parser.add_argument("--mapped", action="store_true", help="also show the DSpace-mapped fields")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
