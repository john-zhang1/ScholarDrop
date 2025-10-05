#!/usr/bin/env python
"""Resolve an identifier against the registries and print what came back.

    .venv/bin/python scripts/resolve_cli.py 10.1038/nature12373
    .venv/bin/python scripts/resolve_cli.py arXiv:1706.03762 --json
    .venv/bin/python scripts/resolve_cli.py 23193287 --mapped

For eyeballing the fallback path of DESIGN.md §13.1 -- the one that runs when the
repository has no live-import provider for an identifier, which on a stock Tomcat
is every DOI. It makes real calls to doi.org, Crossref, DataCite, arXiv and
PubMed, and touches no DSpace at all.

`--sources <base-url>` instead asks a *repository* which providers it has
configured, which is the other half of the same question: what it can resolve for
itself.
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
from scholardrop.dspace import DSpaceClient, DSpaceError  # noqa: E402
from scholardrop.extract.mapper import map_metadata  # noqa: E402
from scholardrop.extract.models import ExtractedMetadata  # noqa: E402
from scholardrop.identifiers import parse_identifier  # noqa: E402
from scholardrop.identifiers.ids import UnknownIdentifier  # noqa: E402
from scholardrop.identifiers.resolvers import RegistryUnavailable, resolve  # noqa: E402
from scholardrop.identifiers.sources import candidates  # noqa: E402
from scholardrop.settings import load_settings  # noqa: E402

DIM, BOLD, YELLOW, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[0m"


def print_human(metadata: ExtractedMetadata, elapsed: float) -> None:
    print(f"{DIM}tier: {metadata.tier_used.value} · {elapsed:.1f}s{RESET}")
    for name, extracted in metadata.present_fields().items():
        value = extracted.value
        text = ", ".join(value) if isinstance(value, list) else str(value)
        if len(text) > 300:
            text = text[:300] + "…"
        print(f"  {BOLD}{name}{RESET}: {text}")
    if not metadata.present_fields():
        print(f"{YELLOW}  the record carried nothing we map{RESET}")


async def show_sources(base_url: str, identifier_text: str | None) -> int:
    """Which live-import providers a repository has, anonymously."""
    async with DSpaceClient(f"{base_url.rstrip('/')}/api") as client:
        try:
            sources = await client.list_external_sources()
        except DSpaceError as exc:
            print(f"{YELLOW}could not read the providers: {exc}{RESET}")
            return 1
    print(f"{BOLD}{len(sources)} provider(s){RESET}: {', '.join(s.id for s in sources)}")
    if identifier_text:
        identifier = parse_identifier(identifier_text)
        usable = candidates(sources, identifier)
        print(
            f"for {identifier.display}: "
            + (", ".join(s.id for s in usable) if usable else f"{YELLOW}none{RESET}")
        )
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifier", nargs="?", help="DOI, arXiv ID, PubMed ID, or a URL")
    parser.add_argument("--json", action="store_true", help="print the raw model")
    parser.add_argument("--mapped", action="store_true", help="also show the DSpace fields")
    parser.add_argument("--profile", default="shareok-etd", help="mapping profile for --mapped")
    parser.add_argument(
        "--sources", metavar="BASE_URL", help="list a repository's live-import providers instead"
    )
    args = parser.parse_args()

    if args.sources:
        return await show_sources(args.sources, args.identifier)
    if not args.identifier:
        parser.error("give an identifier, or --sources BASE_URL")

    try:
        identifier = parse_identifier(args.identifier)
    except UnknownIdentifier as exc:
        print(f"{YELLOW}{exc}{RESET}")
        return 2

    print(f"{BOLD}{identifier.display}{RESET} {DIM}({identifier.url}){RESET}")
    settings = load_settings()
    started = time.monotonic()
    try:
        metadata = await resolve(identifier, settings)
    except RegistryUnavailable as exc:
        print(f"{YELLOW}{exc}{RESET}")
        return 1
    elapsed = time.monotonic() - started

    if args.json:
        print(json.dumps(metadata.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print_human(metadata, elapsed)

    if args.mapped:
        profile = load_registry(settings.config_dir).profiles[args.profile]
        mapped = map_metadata(metadata, profile)
        print(f"\n{BOLD}mapped ({profile.name}){RESET}")
        for field, values in mapped.fields.items():
            print(f"  {field}: {' | '.join(values)}")
        for name, values in mapped.unmapped.items():
            print(f"{DIM}  unmapped {name}: {' | '.join(values)}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
