"""Phase 12 gate: access conditions against a live DSpace (DESIGN.md §13.5).

**This one writes.** It deposits two real items — one open, one embargoed — and
then asks the repository, anonymously, whether it will hand over each file. That
last step is the whole point: an embargo that is recorded but not enforced looks
identical in the submission UI and is worthless.

    .venv/bin/python scripts/gate_phase12.py --base-url http://localhost:8080/server \
        --user admin@gmail.com --password dspace

Deposit into a collection that archives immediately, or the items sit in a review
queue and no policy is applied to anything yet. The two items are disposable test
data; their handles are printed so they can be withdrawn afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.dspace import DSpaceClient, DSpaceError  # noqa: E402
from scholardrop.submission.access import (  # noqa: E402
    AccessChoice,
    build_operations,
    parse_options,
)
from scholardrop.submission.definition import (  # noqa: E402
    ConfigCache,
    load_form_configs,
    load_sections,
    load_upload_config,
    upload_section,
)
from scholardrop.submission.form import UploadedFile  # noqa: E402
from scholardrop.submission.patch import field_index  # noqa: E402

INSTANCE_KEY = "gate"
# The smallest thing PyMuPDF and DSpace both accept as a PDF.
PDF = b"%PDF-1.7\n%\xc7\xec\x8f\xa2\ntrailer<</Root 1 0 R>>\n%%EOF\n"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080/server")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--collection", default="", help="uuid; defaults to the first authorized")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    embargo_until = (date.today() + timedelta(days=365)).isoformat()

    async with DSpaceClient(base_url + "/api", timeout=args.timeout) as client:
        status = await client.login(args.user, args.password)
        print(f"signed in as {status.eperson.name if status.eperson else '?'}")

        collection_uuid = args.collection
        if not collection_uuid:
            page = await client.find_submit_authorized(size=1)
            if not page.items:
                print("no collection this account may submit to")
                return 1
            collection_uuid = page.items[0].uuid
            print(f"using collection {page.items[0].name} ({collection_uuid})")

        cache = ConfigCache()
        options, section_id = await read_options(client, collection_uuid, cache)
        print(f"\n== the upload step ({section_id}) offers:")
        for option in options:
            print(
                f"  {option.name:<14} start={option.has_start_date} end={option.has_end_date} "
                f"max={option.max_date or '-'}"
            )
        assert options, "this collection configures no access conditions; nothing to gate"

        open_item = await deposit(
            client,
            collection_uuid,
            section_id,
            options,
            "Phase 12 gate — open",
            "openaccess",
            "",
            cache,
        )
        embargoed = await deposit(
            client,
            collection_uuid,
            section_id,
            options,
            "Phase 12 gate — embargoed",
            "embargo",
            embargo_until,
            cache,
        )

        print("\n== what an anonymous reader gets")
        open_status = await anonymous_status(base_url, open_item["bitstream"])
        held_status = await anonymous_status(base_url, embargoed["bitstream"])
        print(f"  open file      -> HTTP {open_status}")
        print(f"  embargoed file -> HTTP {held_status} (embargo until {embargo_until})")

        assert open_status == 200, "an open-access file must be readable by anybody"
        assert held_status in (401, 403), (
            "the embargoed file was served to an anonymous reader: the condition was "
            "recorded but is not being enforced"
        )

        print("\ndeposited (disposable test data):")
        for label, item in (("open", open_item), ("embargoed", embargoed)):
            print(f"  {label:<10} item {item['uuid']} handle {item['handle'] or '(pending)'}")

    print("\nall checks passed")
    return 0


async def read_options(client: DSpaceClient, collection_uuid: str, cache: ConfigCache):
    """The conditions this collection offers, via a throwaway draft."""
    draft = await client.create_workspaceitem(collection_uuid)
    try:
        sections = await load_sections(client, draft, cache=cache, instance_key=INSTANCE_KEY)
        section_id, _href = upload_section(sections)
        config = await load_upload_config(client, sections, cache=cache, instance_key=INSTANCE_KEY)
        return parse_options(config), section_id
    finally:
        await client.delete_workspaceitem(draft.id)


async def deposit(
    client: DSpaceClient,
    collection_uuid: str,
    section_id: str,
    options,
    title: str,
    condition: str,
    chosen_date: str,
    cache: ConfigCache,
) -> dict:
    """One real deposit with one access condition, and the file it produced."""
    print(f"\n== depositing {title!r} as {condition} {chosen_date}")
    draft = await client.create_workspaceitem(collection_uuid)
    item = await client.upload_bitstream(draft.id, "gate.pdf", PDF)
    print(f"  draft {draft.id}, {len(item.sections.get(section_id, {}).get('files', []))} file(s)")

    # Which section configures a field is the definition's answer, not a guess
    # from the order the sections happen to arrive in: on this instance that
    # order puts `traditionalpagetwo` first, and patching dc.title into it is a
    # 422 with no clue as to which of the operations was the bad one.
    index = await field_index_for(client, item, cache)

    def path_for(field: str) -> str:
        section = next((s for (s, f) in index if f == field), "")
        return f"/sections/{section}/{field}" if section else ""

    operations = [
        {"op": "add", "path": path_for("dc.title"), "value": [{"value": title}]},
        {"op": "add", "path": path_for("dc.date.issued"), "value": [{"value": "2026"}]},
        {"op": "add", "path": path_for("dc.type"), "value": [{"value": "Article"}]},
        {"op": "add", "path": "/sections/license/granted", "value": "true"},
    ]
    operations = [op for op in operations if op["path"]]
    operations.extend(
        build_operations(
            section_id,
            {0: AccessChoice(index=0, name=condition, chosen_date=chosen_date)},
            options,
            files=[
                UploadedFile.from_payload(f)
                for f in item.sections.get(section_id, {}).get("files", [])
            ],
        )
    )
    patched = await client.patch_sections(draft.id, operations)

    # Fill whatever else this collection insists on, as gate_phase10 does.
    for _round in range(3):
        missing = sorted({f.split("/", 1)[-1] for i in patched.errors for f in i.fields()})
        missing = [f for f in missing if "." in f and path_for(f)]
        if not missing:
            break
        print(f"  filling {missing}")
        patched = await client.patch_sections(
            draft.id,
            [
                {"op": "add", "path": path_for(field), "value": [{"value": "Phase 12 gate"}]}
                for field in missing
            ],
        )
    assert not patched.has_errors, f"draft {draft.id} is still incomplete: {patched.errors}"

    item_link = (patched.raw.get("_links") or {}).get("item", {}).get("href")
    resolved = await client.item(item_link)
    result = await client.deposit(patched.self_link or draft.self_link)
    print(f"  {'archived' if result.archived else 'entered a review workflow'}")
    assert result.archived, (
        "this collection reviews submissions, so no policy is applied yet — "
        "run the gate against one that archives immediately"
    )

    archived = await client.item(resolved.uuid)
    bitstreams = await client.bitstreams_in_bundle(resolved.uuid, "ORIGINAL")
    assert bitstreams, "the deposited item has no file to check"
    print(f"  item {resolved.uuid} handle {getattr(archived, 'handle', '') or '(pending)'}")
    await print_policies(client, bitstreams[0].uuid)
    return {
        "uuid": resolved.uuid,
        "handle": getattr(archived, "handle", ""),
        "bitstream": bitstreams[0].uuid,
    }


async def field_index_for(client: DSpaceClient, item, cache: ConfigCache) -> dict:
    """Every (section, metadata field) this collection's form configures."""
    sections = await load_sections(client, item, cache=cache, instance_key=INSTANCE_KEY)
    configs = await load_form_configs(client, sections, cache=cache, instance_key=INSTANCE_KEY)
    return field_index(sections, configs)


async def print_policies(client: DSpaceClient, bitstream_uuid: str) -> None:
    """The file's resource policies, as the repository now holds them."""
    try:
        payload = await client._json(  # noqa: SLF001 -- a gate script, not app code
            "GET",
            "/authz/resourcepolicies/search/resource",
            params={"uuid": bitstream_uuid},
        )
    except DSpaceError as exc:
        print(f"  (could not read the file's policies: {exc})")
        return
    policies = (payload.get("_embedded") or {}).get("resourcepolicies") or []
    for policy in policies:
        print(
            f"  policy {policy.get('name') or policy.get('policyType')}: "
            f"action={policy.get('action')} start={policy.get('startDate')} "
            f"end={policy.get('endDate')}"
        )


async def anonymous_status(base_url: str, bitstream_uuid: str) -> int:
    """What the file answers with *no* credentials at all -- the real test."""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as anonymous:
        response = await anonymous.get(f"{base_url}/api/core/bitstreams/{bitstream_uuid}/content")
        return response.status_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
