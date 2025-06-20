#!/usr/bin/env python
"""Read-only smoke test of DSpaceClient against a real instance.

Nothing here writes: no workspaceitem is created, nothing is deposited, nothing is
deleted. Safe to point at a production repository, though a test instance is the
polite choice.

    export SCHOLARDROP_SMOKE_BASE_URL=https://shareok.org/server
    export SCHOLARDROP_SMOKE_USER=you@ou.edu
    export SCHOLARDROP_SMOKE_PASSWORD='...'
    .venv/bin/python scripts/smoke.py

BASE_URL is the server root; /api is appended. Credentials are optional -- without
them the script runs the anonymous checks only (status and the community tree) and
skips the authenticated ones. Pass a configured instance key instead of a URL with
`--instance shareok` to read the base URL from config/instances.yaml.

Sections observed in a real workspaceitem are printed at the end when credentials
and --collection are supplied; paste them into the Phase 1 gate note for Phase 4.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.config import load_registry  # noqa: E402
from scholardrop.dspace import DSpaceClient, DSpaceError  # noqa: E402
from scholardrop.settings import load_settings  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}  ok{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}warn{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}FAIL{RESET} {msg}")


def step(title: str) -> None:
    print(f"\n{DIM}--- {title} ---{RESET}")


def resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url
    if args.instance:
        settings = load_settings()
        registry = load_registry(settings.config_dir)
        instance = registry.instance(args.instance)
        if instance is None:
            raise SystemExit(
                f"no instance {args.instance!r} in {settings.config_dir}/instances.yaml; "
                f"known: {', '.join(sorted(registry.instances))}"
            )
        print(f"{DIM}instance {instance.key}: {instance.name}{RESET}")
        return instance.base_url
    env = os.environ.get("SCHOLARDROP_SMOKE_BASE_URL")
    if env:
        return env
    raise SystemExit(
        "give a base URL: --base-url https://host/server, --instance <key>, "
        "or SCHOLARDROP_SMOKE_BASE_URL"
    )


async def run(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args).rstrip("/")
    user = args.user or os.environ.get("SCHOLARDROP_SMOKE_USER")
    password = os.environ.get("SCHOLARDROP_SMOKE_PASSWORD")
    problems = 0

    print(f"target: {base_url}/api")
    if not (user and password):
        warn(
            "no credentials (SCHOLARDROP_SMOKE_USER / SCHOLARDROP_SMOKE_PASSWORD) -- "
            "running anonymous checks only"
        )

    async with DSpaceClient(f"{base_url}/api", timeout=args.timeout) as client:
        step("authn/status (anonymous)")
        try:
            status = await client.status()
            ok(f"reachable; authenticated={status.authenticated}")
            if client.xsrf_token:
                ok("CSRF token captured")
            else:
                warn("no CSRF token in the response -- modifying calls would fail")
                problems += 1
        except DSpaceError as exc:
            fail(str(exc))
            return 1

        step("core/communities/search/top")
        try:
            top = await client.top_communities(size=args.size)
            ok(f"{top.total} top-level communit{'y' if top.total == 1 else 'ies'}")
            for community in top.items[: args.size]:
                print(f"       {community.name}  [{community.handle}]  {community.uuid}")
        except DSpaceError as exc:
            fail(str(exc))
            problems += 1
            top = None

        if top and top.items:
            first = top.items[0]
            step(f"tree under {first.name!r}")
            try:
                subs = await client.subcommunities(first.uuid, size=args.size)
                cols = await client.collections_of(first.uuid, size=args.size)
                ok(f"{subs.total} subcommunities, {cols.total} collections")
                for c in cols.items[: args.size]:
                    print(f"       collection: {c.name}  [{c.handle}]  {c.uuid}")
            except DSpaceError as exc:
                fail(str(exc))
                problems += 1

        if not (user and password):
            step("skipped (no credentials)")
            print("       authn/login, findSubmitAuthorized, canSubmit, findBySubmitter")
            return 1 if problems else 0

        step("authn/login")
        try:
            status = await client.login(user, password)
        except DSpaceError as exc:
            fail(f"login failed: {exc}")
            return 1
        who = status.eperson.display_name if status.eperson else "?"
        ok(f"signed in as {who}")

        step("collections/search/findSubmitAuthorized")
        try:
            authorized = await client.find_submit_authorized(query=args.query, size=args.size)
            if authorized.total == 0:
                warn("this account may not submit to any collection")
            else:
                ok(f"{authorized.total} submittable collection(s)")
            for c in authorized.items[: args.size]:
                print(f"       {c.name}  [{c.handle}]  {c.uuid}")
        except DSpaceError as exc:
            fail(str(exc))
            problems += 1
            authorized = None

        step("authz/authorizations/search/object (feature=canSubmit)")
        probe = args.collection or (
            authorized.items[0].uuid if authorized and authorized.items else None
        )
        if probe:
            try:
                granted = await client.can_submit(client.collection_uri(probe))
                ok(f"canSubmit({probe}) -> {granted}")
                if (
                    authorized
                    and authorized.items
                    and probe == authorized.items[0].uuid
                    and not granted
                ):
                    warn("findSubmitAuthorized and canSubmit disagree -- worth investigating")
                    problems += 1
            except DSpaceError as exc:
                fail(str(exc))
                problems += 1
        else:
            warn("no collection to probe; pass --collection <uuid>")

        step("submission/workspaceitems/search/findBySubmitter")
        try:
            drafts = await client.find_by_submitter(size=args.size)
            ok(f"{drafts.total} in-progress submission(s)")
            for d in drafts.items[: args.size]:
                print(f"       workspaceitem {d.id}  sections={d.section_ids}")
        except DSpaceError as exc:
            fail(str(exc))
            problems += 1

        if args.inspect_sections:
            step("section names from an existing workspaceitem (read-only)")
            try:
                drafts = await client.find_by_submitter(size=1)
                if drafts.items:
                    item = await client.get_workspaceitem(drafts.items[0].id)
                    ok(f"workspaceitem {item.id}")
                    print(f"       data sections:    {item.section_ids}")
                    print(f"       defined sections: {item.defined_section_ids}")
                else:
                    warn(
                        "no existing draft to inspect. Start a submission in the DSpace UI "
                        "and re-run with --inspect-sections, or read the section names from "
                        "item-submission.xml"
                    )
            except DSpaceError as exc:
                fail(str(exc))
                problems += 1

    print()
    if problems:
        fail(f"{problems} check(s) need attention")
        return 1
    ok("all checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", help="DSpace server root, e.g. https://shareok.org/server")
    parser.add_argument("--instance", help="instance key from config/instances.yaml")
    parser.add_argument("--user", help="EPerson email (or SCHOLARDROP_SMOKE_USER)")
    parser.add_argument("--collection", help="collection uuid to probe with canSubmit")
    parser.add_argument("--query", help="search term for findSubmitAuthorized")
    parser.add_argument("--size", type=int, default=5, help="page size / rows printed (default 5)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--inspect-sections",
        action="store_true",
        help="print section names from an existing draft (for the Phase 4 form)",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
