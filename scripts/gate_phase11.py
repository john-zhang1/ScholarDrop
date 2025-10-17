"""Phase 11 gate: the metadata quality helpers against a live DSpace (§13.4).

Each slice is exercised where it actually meets the repository, because that is
where the assumptions are:

1. **defaults / submit another like this** -- a draft is created, defaults are
   written into it, a second draft is seeded from the first's values, and both
   are deleted. Proves the patch shape and the "never overwrite" rule against a
   real submission form.
2. **vocabulary type-ahead** -- reports which fields this repository binds to a
   controlled vocabulary, and whether each vocabulary can be listed, searched,
   or neither. This is the "note which SHAREOK fields actually bind
   vocabularies" the gate asks for.
3. **scanned-PDF warning** -- runs the reader over a PDF and prints what it
   would say. Local; pass `--pdf` to point it at a real one.
4. **bilingual** -- detects the language of that PDF, then patches a draft with
   two values of one field carrying different `language` keys and reads them
   back. Proves DSpace stores the key rather than dropping it.

    .venv/bin/python scripts/gate_phase11.py --base-url http://localhost:8080/server \
        --user admin@gmail.com --password dspace --pdf ../Xu_oklahoma_2409A_10865.pdf

Read-mostly: every draft it creates is deleted again, and nothing is deposited.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.config import MappingProfile, ProfileDefaults  # noqa: E402
from scholardrop.dspace import DSpaceClient, DSpaceError  # noqa: E402
from scholardrop.extract.language import detect_language  # noqa: E402
from scholardrop.extract.reader import read_pdf  # noqa: E402
from scholardrop.submission.defaults import apply_defaults, apply_seed, carry_over  # noqa: E402
from scholardrop.submission.definition import (  # noqa: E402
    ConfigCache,
    load_form_configs,
    load_sections,
)
from scholardrop.submission.patch import field_index  # noqa: E402

INSTANCE_KEY = "gate"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080/server")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    # See gate_phase10.py: a password login on this VM waits out an unreachable
    # LDAP server before it reaches PasswordAuthentication.
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--collection", default="", help="uuid; defaults to the first authorized")
    parser.add_argument("--pdf", default="", help="a real PDF for the reader and detector checks")
    args = parser.parse_args()

    if args.pdf:
        check_document(Path(args.pdf))

    async with DSpaceClient(args.base_url.rstrip("/") + "/api", timeout=args.timeout) as client:
        status = await client.login(args.user, args.password)
        who = status.eperson
        print(f"\nsigned in as {who.name if who else '?'}")

        collection_uuid = args.collection
        if not collection_uuid:
            page = await client.find_submit_authorized(size=1)
            if not page.items:
                print("no collection this account may submit to")
                return 1
            collection_uuid = page.items[0].uuid
            print(f"using collection {page.items[0].name} ({collection_uuid})")

        cache = ConfigCache()
        await check_defaults(client, collection_uuid, cache)
        await check_vocabularies(client, collection_uuid, cache)
        await check_parallel_values(client, collection_uuid, cache)

    print("\nall checks passed")
    return 0


# ---- slice 3 and half of slice 4, both local -------------------------------


def check_document(path: Path) -> None:
    print(f"== reader: {path.name}")
    document = read_pdf(path)
    print(f"  pages={document.page_count} text={len(document.front_text)} chars")
    print(f"  probably_scanned={document.probably_scanned}")
    code, confidence = detect_language(document.text_for_llm)
    print(f"  detected language={code or '(none)'} confidence={confidence}")


# ---- slice 1 ----------------------------------------------------------------


async def check_defaults(client: DSpaceClient, collection_uuid: str, cache: ConfigCache) -> None:
    print("\n== defaults and 'submit another like this'")
    profile = MappingProfile(
        name="gate",
        fields={"title": "dc.title", "abstract": "dc.description.abstract"},
        defaults=ProfileDefaults.parse({"dc.type": "Article", "dc.language.iso": "en"}),
    )
    draft = await client.create_workspaceitem(collection_uuid)
    print(f"  created draft {draft.id}")
    try:
        applied = await apply_defaults(
            client, draft, profile=profile, cache=cache, instance_key=INSTANCE_KEY
        )
        print(f"  applied {applied}")
        after = await client.get_workspaceitem(draft.id)
        stored = {
            field: [v.get("value") for v in values]
            for section, data in after.sections.items()
            if isinstance(data, dict)
            for field, values in data.items()
            if isinstance(values, list) and field in applied
        }
        print(f"  read back {stored}")
        assert stored, "the repository kept none of the defaults"

        # A default must never overwrite what is already there.
        again = await apply_defaults(
            client, after, profile=profile, cache=cache, instance_key=INSTANCE_KEY
        )
        print(f"  applied a second time: {again} (must be empty)")
        assert not again

        # And the seed for the next submission carries the context, not the work.
        submitted = {
            ("x", "dc.title"): ["Phase 11 gate"],
            ("x", "dc.type"): ["Article"],
        }
        carried = carry_over(submitted, profile)
        print(f"  carry_over -> {carried} (no title)")
        assert not any(key.endswith("dc.title") for key in carried)

        seeded = await client.create_workspaceitem(collection_uuid)
        try:
            index = await _index(client, seeded, cache)
            section = next(s for (s, f) in index if f == "dc.type")
            written = await apply_seed(
                client,
                seeded,
                {f"{section}::dc.type": ["Article"]},
                cache=cache,
                instance_key=INSTANCE_KEY,
            )
            print(f"  seeded draft {seeded.id} with {written}")
            assert written == {"dc.type": ["Article"]}
        finally:
            await client.delete_workspaceitem(seeded.id)
    finally:
        await client.delete_workspaceitem(draft.id)
        print(f"  deleted draft {draft.id}")


# ---- slice 2 ----------------------------------------------------------------


async def check_vocabularies(
    client: DSpaceClient, collection_uuid: str, cache: ConfigCache
) -> None:
    print("\n== controlled vocabularies this collection's form binds")
    draft = await client.create_workspaceitem(collection_uuid)
    try:
        index = await _index(client, draft, cache)
    finally:
        await client.delete_workspaceitem(draft.id)

    bound = {config.vocabulary: field for (_s, field), config in index.items() if config.vocabulary}
    if not bound:
        print("  none — this collection's form binds no vocabulary")
        return
    for name, field in sorted(bound.items()):
        listable = await _listable(client, name)
        searchable = await _searchable(client, name)
        print(f"  {name:<16} bound to {field:<24} listable={listable} searchable={searchable}")
        assert listable or searchable, f"{name} can be neither listed nor searched"


async def _listable(client: DSpaceClient, name: str) -> bool:
    try:
        return bool(await client.vocabulary_entries(name, size=1))
    except DSpaceError:
        # 422 "not scrollable" is the normal answer for a vocabulary backed by an
        # XML file, and is exactly what the type-ahead exists for.
        return False


async def _searchable(client: DSpaceClient, name: str) -> bool:
    for probe in ("a", "e", "s"):
        try:
            if await client.vocabulary_matches(name, probe, size=3):
                return True
        except DSpaceError:
            return False
    return False


# ---- slice 4 ----------------------------------------------------------------


async def check_parallel_values(
    client: DSpaceClient, collection_uuid: str, cache: ConfigCache
) -> None:
    print("\n== parallel values with a language key")
    draft = await client.create_workspaceitem(collection_uuid)
    print(f"  created draft {draft.id}")
    try:
        index = await _index(client, draft, cache)
        section = next(s for (s, f) in index if f == "dc.title")
        await client.patch_sections(
            draft.id,
            [
                {
                    "op": "add",
                    "path": f"/sections/{section}/dc.title",
                    "value": [
                        {"value": "泥沙运移研究", "language": "zh"},
                        {"value": "A Study of Sediment Transport", "language": "en"},
                    ],
                }
            ],
        )
        after = await client.get_workspaceitem(draft.id)
        values = (after.sections.get(section) or {}).get("dc.title") or []
        for entry in values:
            print(f"  {entry.get('value')!r} language={entry.get('language')!r}")
        assert [e.get("language") for e in values] == ["zh", "en"], (
            "DSpace did not keep the language keys, so a translation would be "
            "deposited as a second title"
        )
    finally:
        await client.delete_workspaceitem(draft.id)
        print(f"  deleted draft {draft.id}")


async def _index(client: DSpaceClient, item, cache: ConfigCache) -> dict:
    sections = await load_sections(client, item, cache=cache, instance_key=INSTANCE_KEY)
    configs = await load_form_configs(client, sections, cache=cache, instance_key=INSTANCE_KEY)
    return field_index(sections, configs)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
