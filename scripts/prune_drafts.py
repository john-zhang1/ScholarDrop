#!/usr/bin/env python
"""Delete your own empty in-progress submissions.

DSpace creates a workspaceitem the moment a submission is started, so every
abandoned start leaves a draft behind for ever. On a long-lived repository that
accumulates: this VM's database holds 372 drafts with no metadata and no file, the
oldest from 2015. They are invisible clutter in `/drafts` and in MyDSpace.

    export SCHOLARDROP_PRUNE_USER=you@example.edu
    export SCHOLARDROP_PRUNE_PASSWORD='...'
    .venv/bin/python scripts/prune_drafts.py --instance local            # dry run
    .venv/bin/python scripts/prune_drafts.py --instance local --apply    # delete

**It can only ever delete drafts belonging to the account it signs in as.** The
list comes from `findBySubmitter` with that account's own uuid, so there is no
flag, and no mistake, that reaches somebody else's submissions. Pruning another
user's drafts means signing in as them.

**Empty means empty**: no metadata value in any section, no file attached, and the
deposit licence not granted. Anything a person has touched is left alone, and a
draft that cannot be read is left alone too -- being unable to tell is a reason to
skip, not a reason to guess.

Nothing is deleted without `--apply`. Take a database backup first anyway; this
calls DSpace's own delete, which is thorough and final.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.config import load_registry  # noqa: E402
from scholardrop.dspace import DSpaceClient, DSpaceError  # noqa: E402
from scholardrop.dspace.models import WorkspaceItem  # noqa: E402
from scholardrop.routes.drafts import format_timestamp  # noqa: E402
from scholardrop.settings import load_settings  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

PAGE_SIZE = 50
# Sections that are structure rather than content: `collection` is a uuid string,
# and `upload`/`license` are inspected on their own terms below.
NON_METADATA_SECTIONS = frozenset({"collection", "upload", "license"})


@dataclass(frozen=True)
class Verdict:
    """Whether one draft is empty, and what it is holding if not."""

    empty: bool
    reason: str = ""


def inspect(item: WorkspaceItem) -> Verdict:
    """Decide whether a draft holds anything a person would miss.

    Conservative by construction: every branch that is not certain the draft is
    empty returns "keep". The cost of keeping one too many is a row in a list; the
    cost of deleting one too many is somebody's work.
    """
    sections = item.sections
    if not isinstance(sections, dict) or not sections:
        return Verdict(False, "no section data to judge by")

    for name, data in sections.items():
        if name in NON_METADATA_SECTIONS or not isinstance(data, dict):
            continue
        for field_name, entries in data.items():
            if isinstance(entries, list) and any(
                isinstance(e, dict) and str(e.get("value", "")).strip() for e in entries
            ):
                return Verdict(False, f"has {field_name}")

    upload = sections.get("upload")
    if isinstance(upload, dict) and (upload.get("files") or []):
        return Verdict(False, "has a file")

    licence = sections.get("license")
    if isinstance(licence, dict) and licence.get("granted"):
        return Verdict(False, "licence granted")

    if item.errors and not any(
        issue.message == "error.validation.required" for issue in item.errors
    ):
        # Complaints beyond "you have not filled this in" mean something was done
        # to this draft that is not understood here.
        return Verdict(False, "unexpected validation errors")

    return Verdict(True)


def age_days(item: WorkspaceItem) -> float | None:
    raw = (item.last_modified or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() / 86400


async def collect(client: DSpaceClient, uuid: str) -> list[WorkspaceItem]:
    """Every draft this account has, following pagination."""
    items: list[WorkspaceItem] = []
    page = 0
    while True:
        result = await client.find_by_submitter(uuid, page=page, size=PAGE_SIZE)
        items.extend(result.items)
        if not result.has_next:
            return items
        page += 1


async def run(args: argparse.Namespace) -> int:
    base_url = args.base_url
    if args.instance:
        registry = load_registry(load_settings(secret_key="prune").config_dir)
        instance = registry.instance(args.instance)
        if instance is None:
            print(f"{RED}no instance {args.instance!r} in config/instances.yaml{RESET}")
            return 2
        base_url = instance.base_url
    if not base_url:
        print(f"{RED}give --instance or --base-url{RESET}")
        return 2

    user = os.environ.get("SCHOLARDROP_PRUNE_USER", "")
    password = os.environ.get("SCHOLARDROP_PRUNE_PASSWORD", "")
    if not (user and password):
        print(f"{RED}set SCHOLARDROP_PRUNE_USER and SCHOLARDROP_PRUNE_PASSWORD{RESET}")
        return 2

    async with DSpaceClient(f"{base_url}/api") as client:
        status = await client.login(user, password)
        if not status.authenticated:
            print(f"{RED}sign-in refused for {user}{RESET}")
            return 1
        uuid = status.eperson.uuid if status.eperson else ""
        if not uuid:
            print(f"{RED}signed in but the account has no uuid; cannot list its drafts{RESET}")
            return 1
        print(f"{BOLD}{user}{RESET} at {base_url}")

        drafts = await collect(client, uuid)
        empty, kept = [], []
        for item in drafts:
            verdict = inspect(item)
            recent = (age_days(item) or 0) < args.min_age_days
            if verdict.empty and not recent:
                empty.append(item)
            else:
                kept.append((item, "changed recently" if verdict.empty else verdict.reason))

        print(f"{len(drafts)} draft(s); {len(empty)} empty, {len(kept)} kept\n")
        for item, reason in kept[: args.show]:
            when = format_timestamp(item.last_modified)
            print(f"{DIM}  keep   {item.id:<8} {when:<22} {reason}{RESET}")
        if len(kept) > args.show:
            print(f"{DIM}  keep   … and {len(kept) - args.show} more{RESET}")
        for item in empty:
            print(f"  delete {item.id:<8} {format_timestamp(item.last_modified):<22} empty")

        if not empty:
            print(f"\n{GREEN}nothing to do{RESET}")
            return 0
        if not args.apply:
            print(f"\n{YELLOW}dry run — nothing deleted. Re-run with --apply.{RESET}")
            return 0
        if args.expect is not None and len(empty) != args.expect:
            print(
                f"\n{RED}expected {args.expect} empty draft(s), found {len(empty)}; "
                f"refusing to delete{RESET}"
            )
            return 1

        deleted, failed = 0, 0
        for item in empty:
            try:
                await client.delete_workspaceitem(item.id)
            except DSpaceError as exc:
                failed += 1
                print(f"{RED}  failed {item.id}: {exc}{RESET}")
            else:
                deleted += 1
        print(
            f"\n{GREEN}deleted {deleted}{RESET}"
            + (f", {RED}{failed} failed{RESET}" if failed else "")
        )
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--instance", help="key from config/instances.yaml")
    parser.add_argument("--base-url", help="DSpace server root, if not using --instance")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument(
        "--min-age-days",
        type=float,
        default=1.0,
        help="never touch a draft changed more recently than this (default: 1)",
    )
    parser.add_argument(
        "--expect",
        type=int,
        help="refuse to delete unless exactly this many empty drafts are found",
    )
    parser.add_argument("--show", type=int, default=10, help="how many kept drafts to list")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
