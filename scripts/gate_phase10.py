"""Phase 10 gate: item_status against a live DSpace (DESIGN.md §13.3).

Read-mostly. It creates one throwaway workspaceitem to exercise the "back with
its submitter" branch and deletes it again; everything else is a read.

    .venv/bin/python scripts/gate_phase10.py --base-url http://localhost:8080/server \
        --user admin@gmail.com --password dspace
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.dspace import DSpaceClient  # noqa: E402
from scholardrop.history.status import state_of  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080/server")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    # Long, because on this VM `LDAPAuthentication` is first in
    # plugin.sequence.org.dspace.authenticate.AuthenticationMethod and the OU
    # directory is unreachable from here -- every password login waits for that
    # connect to time out (~80s) before falling through to PasswordAuthentication.
    # Nothing to do with the endpoints under test; the reads below are instant.
    parser.add_argument("--timeout", type=float, default=180.0)
    # A collection with a staffed workflow role, to reach the one state that
    # cannot be produced by depositing anywhere that archives immediately.
    parser.add_argument("--workflow-collection", default="")
    args = parser.parse_args()

    async with DSpaceClient(args.base_url.rstrip("/") + "/api", timeout=args.timeout) as client:
        status = await client.login(args.user, args.password)
        who = status.eperson
        print(f"signed in as {who.name if who else '?'} ({who.uuid if who else '?'})")

        # 1. An archived item -> Live, with its handle, in one request.
        page = await client.search_items(size=1)
        archived = page.items[0]
        result = await client.item_status(archived.uuid)
        state, detail = state_of(result)
        print(f"\narchived item {archived.uuid}")
        print(f"  -> {state}  handle={result.handle!r}  detail={detail!r}")
        assert state.value == "live", state
        assert result.handle, "an archived item must carry a handle"

        # 2. A uuid no item has -> gone, not an exception.
        missing = await client.item_status("00000000-0000-0000-0000-000000000000")
        print(f"\nnonexistent item -> {state_of(missing)}")
        assert missing.found is False and missing.checked is True

        # 3. A live draft -> Draft, and the workspaceitem it is.
        collections = await client.find_submit_authorized(size=1)
        if not collections.items:
            print("\nno collection this account may submit to; skipping the draft check")
            return 0
        collection = collections.items[0]
        draft = await client.create_workspaceitem(collection.uuid)
        print(f"\ncreated workspaceitem {draft.id} in {collection.name}")
        try:
            # A workspaceitem's `_links.item` is a sub-resource of itself, so the
            # uuid has to be *followed*, exactly as the deposit path does.
            item_link = (draft.raw.get("_links") or {}).get("item", {}).get("href")
            behind = await client.item(item_link)
            print(f"  item behind it: {behind.uuid}")
            result = await client.item_status(behind.uuid)
            state, detail = state_of(result)
            print(f"  -> {state}  workspaceitem_id={result.workspaceitem_id!r}")
            assert state.value == "draft", state
            assert result.workspaceitem_id == draft.id, (
                "the draft must be findable from its item, or a returned submission "
                "cannot be resumed"
            )
        finally:
            await client.delete_workspaceitem(draft.id)
            print(f"  deleted workspaceitem {draft.id}")

        if args.workflow_collection:
            await check_in_review(client, args.workflow_collection)

    print("\nall checks passed")
    return 0


_FILLER = {
    "dc.date.issued": "2026",
    "dc.type": "Article",
    "dc.language": "en",
    "dc.language.iso": "en",
    "dc.rights": "In Copyright",
    "dc.contributor.author": "Gate, Phase Ten",
    "dc.subject": "gate",
}


async def check_in_review(client: DSpaceClient, collection_uuid: str) -> None:
    """Deposit into a collection that reviews, and read the state back.

    The submission is aborted afterwards (`?expunge=true`), so nothing is left in
    anyone's review queue.
    """
    draft = await client.create_workspaceitem(collection_uuid)
    print(f"\ncreated workspaceitem {draft.id} for the review check")
    print(f"  sections: {list(draft.sections)}")
    forms = [
        name
        for name, data in draft.sections.items()
        if isinstance(data, dict) and name not in {"collection", "upload", "license"}
    ]
    section = forms[0] if forms else "traditionalpageone"
    ops = [
        {
            "op": "add",
            "path": f"/sections/{section}/dc.title",
            "value": [{"value": "Phase 10 gate — abort me"}],
        },
        {
            "op": "add",
            "path": f"/sections/{section}/dc.date.issued",
            "value": [{"value": "2026"}],
        },
        {"op": "add", "path": f"/sections/{section}/dc.type", "value": [{"value": "Article"}]},
        {"op": "add", "path": "/sections/license/granted", "value": "true"},
    ]
    patched = await client.patch_sections(draft.id, ops)
    # Which fields a collection requires is its own configuration, so fill
    # whatever it names rather than assuming the shipped `traditional` form.
    for _round in range(3):
        missing = sorted({f.split("/", 1)[-1] for i in patched.errors for f in i.fields()})
        missing = [f for f in missing if f not in {"upload", "license"} and "." in f]
        if not missing:
            break
        print(f"  filling {missing}")
        patched = await client.patch_sections(
            draft.id,
            [
                {
                    "op": "add",
                    "path": f"/sections/{section}/{field}",
                    "value": [{"value": _FILLER.get(field, "Phase 10 gate")}],
                }
                for field in missing
            ],
        )
    if patched.has_errors:
        for issue in patched.errors:
            print(f"  still incomplete: {issue.message} {issue.fields()}")

    item_link = (patched.raw.get("_links") or {}).get("item", {}).get("href")
    behind = await client.item(item_link)
    result = await client.deposit(patched.self_link)
    print(f"  deposit -> {'archived' if result.archived else 'entered workflow'}")
    if result.archived:
        print("  this collection archives immediately; the in-review state was not reached")
        return

    status = await client.item_status(behind.uuid)
    state, _ = state_of(status)
    print(f"  item {behind.uuid} -> {state}  workflow_id={status.workflow_id!r}")
    assert state.value == "in_review", state
    assert status.in_archive is False and not status.handle

    wfi = result.workflow_item.id if result.workflow_item else status.workflow_id
    await client._request(
        "DELETE",
        f"/workflow/workflowitems/{wfi}",
        params={"expunge": "true"},
        expected=(200, 204),
    )
    print(f"  aborted workflowitem {wfi}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
