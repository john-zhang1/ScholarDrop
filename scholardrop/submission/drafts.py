"""One row of a drafts list, built from a workspaceitem.

Lives here rather than in the route because two pages now render drafts: the
drafts list (DESIGN.md §6) and the submission status page (§13.3), where an
in-progress submission is one of the states. Both need the same three things
DSpace does not put in the workspaceitem itself -- the collection's name, the
collection's field labels, and the repository's own verdict on what the draft
still needs -- and both must survive not being able to read any of them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from scholardrop.dspace import DSpaceError
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.submission.definition import load_form_configs, load_sections
from scholardrop.submission.form import UploadedFile
from scholardrop.submission.readiness import Readiness, assess, labels_from
from scholardrop.timestamps import format_timestamp

log = logging.getLogger("scholardrop.drafts")

TITLE_FIELD = "dc.title"


@dataclass
class Draft:
    """One in-progress submission, as a page renders it."""

    id: str
    title: str
    collection_name: str
    last_modified: str
    filenames: list[str]
    readiness: Readiness = field(default_factory=Readiness)
    #: The raw value, for the `<time datetime="...">` attribute.
    last_modified_iso: str = ""


def first_value(sections: dict[str, Any], metadata_field: str) -> str:
    """The first value of a metadata field, wherever in the form it lives."""
    for data in sections.values():
        if not isinstance(data, dict):
            continue
        entries = data.get(metadata_field) or []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("value"):
                    return str(entry["value"])
    return ""


def filenames(sections: dict[str, Any]) -> list[str]:
    """The names of the files a draft holds.

    Read through `UploadedFile`, because **an upload-section file has no `name`
    key**: the filename is `dc.title` in the bitstream's own metadata. Reading
    `entry["name"]` finds nothing and never says so, which is how every draft
    with a file attached was listed as "no file attached".

    Found by shape, not by the section id: a definition may call its upload
    step anything (the access-condition patch path learned this first), and this
    function only has the item's section *data* -- no definition to ask. Any
    section carrying a `files` list is an upload section, and a definition with
    two of them contributes both.
    """
    names = []
    for data in sections.values():
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            continue
        for entry in files:
            if not isinstance(entry, dict):
                continue
            name = UploadedFile.from_payload(entry).name
            if name:
                names.append(name)
    return names


async def labels_for(
    client: Any, item: WorkspaceItem, *, cache: Any, instance_key: str, memo: dict[str, Any]
) -> tuple[dict[str, str], bool]:
    """Field labels for this draft's collection, or nothing and an honest flag.

    Keyed by the submission definition rather than the collection, because two
    collections sharing a definition share their labels. `memo` spans the page;
    the `ConfigCache` underneath spans the hour, so the second page of drafts
    usually fetches nothing.
    """
    definition = (item.raw.get("_links") or {}).get("submissionDefinition", {}).get("href", "")
    key = str(definition or item.sections.get("collection") or item.id)
    if key in memo:
        labels = memo[key]
        return (labels, True) if labels is not None else ({}, False)

    try:
        sections = await load_sections(client, item, cache=cache, instance_key=instance_key)
        configs = await load_form_configs(client, sections, cache=cache, instance_key=instance_key)
        labels = labels_from(configs)
    except DSpaceError as exc:
        # A draft whose form config we cannot read still gets to say what is
        # wrong with it, in DSpace's own field names. Never take the list down.
        log.info("could not read the form config for draft %s: %s", item.id, exc)
        memo[key] = None
        return {}, False

    memo[key] = labels
    return labels, True


async def to_draft(
    client: Any,
    item: WorkspaceItem,
    names: dict[str, str],
    *,
    cache: Any,
    instance_key: str,
    labels_memo: dict[str, Any],
) -> Draft:
    """Build one row. `names` memoises collection names across the page."""
    collection_uuid = item.sections.get("collection")
    collection_name = ""
    if isinstance(collection_uuid, str) and collection_uuid:
        if collection_uuid not in names:
            try:
                names[collection_uuid] = (await client.collection(collection_uuid)).name
            except DSpaceError as exc:
                # A draft whose collection we cannot read is still resumable.
                log.info("could not name collection %s: %s", collection_uuid, exc)
                names[collection_uuid] = ""
        collection_name = names[collection_uuid]

    labels, labelled = await labels_for(
        client, item, cache=cache, instance_key=instance_key, memo=labels_memo
    )
    return Draft(
        id=item.id,
        title=first_value(item.sections, TITLE_FIELD),
        collection_name=collection_name,
        last_modified=format_timestamp(item.last_modified),
        last_modified_iso=item.last_modified or "",
        filenames=filenames(item.sections),
        readiness=assess(item, labels, labelled=labelled),
    )
