"""Values a draft starts with, and a draft seeded from the last one (DESIGN.md §13.4).

Two ways a form arrives part-filled without anybody typing:

* **profile defaults** -- the `defaults:` block of the instance's mapping profile,
  written into a *new* draft at creation (degree level, language, type: the fields
  that are the same for every deposit into a collection);
* **"submit another like this"** -- a new draft seeded from the one just
  deposited, minus the things that identify one particular work.

Both write through the same three rules, and all three exist because a value
nobody typed must never be able to overwrite one somebody did:

1. only a field **this collection's form configures** is ever written, resolved
   live rather than remembered (`field_index`), so a stale seed or a profile
   naming a field this collection has no home for is dropped, not forced;
2. a field the draft **already holds a value in** is left alone -- a draft made
   from a registry record arrives with metadata, and that record outranks a
   default (DESIGN.md §13.1);
3. failure is a log line. A default that cannot be written costs the submitter
   nothing; the form still renders and the draft is still theirs.
"""

from __future__ import annotations

import logging
from typing import Any

from scholardrop.config import MappingProfile
from scholardrop.dspace import DSpaceError
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.submission.definition import ConfigCache, load_form_configs, load_sections
from scholardrop.submission.patch import NAME_SEPARATOR, field_index, stored_values

log = logging.getLogger("scholardrop.submission")

# Extracted-field names whose mapped metadata field belongs to *one* work, and so
# is never carried into the next draft (DESIGN.md §13.4). The plan says "minus
# title, authors and file"; an abstract and a DOI are as particular to a single
# work as its title is, and a carried one is wrong in a way a submitter skimming
# a pre-filled form would not catch. Which metadata field each of these is, is
# the profile's answer -- nothing here matches on `dc.*`.
NOT_CARRIED = ("title", "abstract", "doi")


async def apply_defaults(
    client: Any,
    item: WorkspaceItem,
    *,
    profile: MappingProfile,
    cache: ConfigCache,
    instance_key: str,
) -> dict[str, list[str]]:
    """Write this collection's profile defaults into a newly created draft.

    Returns what was actually written, keyed by metadata field -- empty when the
    profile has no defaults for this collection, when the form has nowhere to put
    them, or when the draft already carries them.
    """
    collection_uuid = item.sections.get("collection")
    wanted = profile.defaults.for_collection(
        collection_uuid if isinstance(collection_uuid, str) else ""
    )
    if not wanted:
        return {}

    index = await _field_index(client, item, cache=cache, instance_key=instance_key)
    targets: dict[tuple[str, str], list[str]] = {}
    for metadata_field, values in wanted.items():
        key = next((k for k in index if k[1] == metadata_field), None)
        if key is None:
            log.info(
                "default %s is not a field the form of collection %s configures",
                metadata_field,
                collection_uuid,
            )
            continue
        targets[key] = values

    applied = await _write(client, item, targets, index)
    if applied:
        log.info(
            "draft %s starts with %s from the %s profile",
            item.id,
            ", ".join(sorted(applied)),
            profile.name,
        )
    return applied


async def apply_seed(
    client: Any,
    item: WorkspaceItem,
    seed: dict[str, list[str]],
    *,
    cache: ConfigCache,
    instance_key: str,
) -> dict[str, list[str]]:
    """Write the values carried over from a previous deposit into a new draft.

    `seed` is keyed `section::metadata.field`, as `carry_over` produced it. The
    keys are re-checked against the live configuration rather than trusted: they
    have been sitting in a session, and the collection may have been reconfigured
    since the deposit they came from.
    """
    if not seed:
        return {}

    index = await _field_index(client, item, cache=cache, instance_key=instance_key)
    targets: dict[tuple[str, str], list[str]] = {}
    for name, values in seed.items():
        section_id, _, metadata_field = str(name).partition(NAME_SEPARATOR)
        key = (section_id, metadata_field)
        if key not in index:
            log.info("seeded value for %s is not configured on this form; dropped", name)
            continue
        targets[key] = list(values)

    applied = await _write(client, item, targets, index)
    if applied:
        log.info("draft %s seeded with %s", item.id, ", ".join(sorted(applied)))
    return applied


def carry_over(
    submitted: dict[tuple[str, str], list[str]], profile: MappingProfile
) -> dict[str, list[str]]:
    """What "submit another like this" takes from the deposit just made.

    Everything the submitter approved except the fields that describe *that*
    work: its title, its authors (and advisor, and committee -- whoever this
    profile says holds a person), its abstract and its DOI. What is left is the
    context a series of related deposits shares: collection, type, language,
    department, publisher, date.
    """
    excluded = {profile.metadata_field(name) for name in NOT_CARRIED}
    excluded |= profile.author_fields()
    return {
        f"{section_id}{NAME_SEPARATOR}{metadata_field}": list(values)
        for (section_id, metadata_field), values in submitted.items()
        if values and metadata_field not in excluded
    }


async def _field_index(
    client: Any, item: WorkspaceItem, *, cache: ConfigCache, instance_key: str
) -> dict[tuple[str, str], Any]:
    sections = await load_sections(client, item, cache=cache, instance_key=instance_key)
    configs = await load_form_configs(client, sections, cache=cache, instance_key=instance_key)
    return field_index(sections, configs)


async def _write(
    client: Any,
    item: WorkspaceItem,
    targets: dict[tuple[str, str], list[str]],
    index: dict[tuple[str, str], Any],
) -> dict[str, list[str]]:
    """Patch the fields that are configured, empty, and have something to write."""
    operations = []
    applied: dict[str, list[str]] = {}
    for (section_id, metadata_field), values in targets.items():
        if stored_values(item, section_id, metadata_field):
            # Whatever is already there was put there by the repository (a
            # registry record) or by the submitter. Neither loses to a default.
            continue
        config = index[(section_id, metadata_field)]
        accepted = values if config.repeatable else values[:1]
        if not accepted:
            continue
        operations.append(
            {
                "op": "add",
                "path": f"/sections/{section_id}/{metadata_field}",
                "value": [{"value": v} for v in accepted],
            }
        )
        applied[metadata_field] = accepted

    if not operations:
        return {}
    try:
        await client.patch_sections(item.id, operations)
    except DSpaceError as exc:
        # Never fatal: the draft exists, the form renders, and the submitter can
        # type what the repository would not take from us.
        log.warning("could not pre-fill draft %s (%s); leaving it empty", item.id, exc)
        return {}
    return applied
