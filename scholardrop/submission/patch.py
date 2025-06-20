"""The edited form -> a JSON Patch DSpace accepts (DESIGN.md §3, §6 step 7).

The operation semantics here are not guesses; they were probed against DSpace 9.2
on 2026-07-28, because getting them wrong is silent data loss:

* `add` on `/sections/{section}/{metadata.field}` with the **whole array** *sets*
  the field -- a second `add` with a different array replaces the first, it does
  not append. That makes one `add` per changed field the honest expression of
  "these are the values now".
* `remove` on a field clears it, and is a **no-op (200) when the field is not
  there** -- so clearing is always safe to send.
* `replace` on a field path answers **500**, whether or not the field exists. It
  is never used.
* `/sections/license/granted` takes the string `"true"`, and DSpace responds by
  filling in the licence bitstream url and acceptance date itself.

Only fields the collection's form actually configures are ever patched: the
submitted names are matched against the live field configuration, so a crafted
POST cannot write a metadata field this collection does not offer.
"""

from __future__ import annotations

import logging
from typing import Any

from scholardrop.dspace.models import WorkspaceItem
from scholardrop.submission.form import (
    FORM_SECTION,
    LICENSE_SECTION,
    parse_fields,
)

log = logging.getLogger("scholardrop.submission")

NAME_SEPARATOR = "::"
LICENSE_FIELD = "license_granted"
LICENSE_PATH = f"/sections/{LICENSE_SECTION}/granted"

# Submitted values, keyed by (section id, metadata field).
Submitted = dict[tuple[str, str], list[str]]


def field_index(
    sections: list[dict[str, Any]], form_configs: dict[str, dict[str, Any]]
) -> dict[tuple[str, str], Any]:
    """Every (section, metadata field) this collection's form configures."""
    index: dict[tuple[str, str], Any] = {}
    for panel in sections:
        section_id = str(panel.get("id"))
        if str(panel.get("sectionType") or "") != FORM_SECTION:
            continue
        for config in parse_fields(form_configs.get(section_id) or {}):
            index[(section_id, config.metadata_field)] = config
    return index


def parse_submission(
    items: list[tuple[str, str]], allowed: dict[tuple[str, str], Any]
) -> Submitted:
    """Read `section::metadata.field` inputs, in order, dropping the unknown.

    `items` is the form's multi-items, so a repeatable field arrives as several
    pairs with the same name and their order is the order the user sees.
    """
    submitted: Submitted = {}
    for name, raw in items:
        if NAME_SEPARATOR not in name:
            continue
        section_id, _, metadata_field = name.partition(NAME_SEPARATOR)
        key = (section_id, metadata_field)
        if key not in allowed:
            log.info("ignoring submitted value for unconfigured field %s", name)
            continue
        value = str(raw).strip()
        if value:
            submitted.setdefault(key, []).append(value)
        else:
            submitted.setdefault(key, [])
    return submitted


def license_granted(items: list[tuple[str, str]]) -> bool:
    """An unchecked checkbox is simply absent from the POST."""
    return any(name == LICENSE_FIELD for name, _ in items)


def stored_values(item: WorkspaceItem, section_id: str, metadata_field: str) -> list[str]:
    section = item.sections.get(section_id)
    if not isinstance(section, dict):
        return []
    entries = section.get(metadata_field) or []
    if not isinstance(entries, list):
        return []
    return [str(e["value"]) for e in entries if isinstance(e, dict) and e.get("value")]


def build_operations(
    item: WorkspaceItem,
    submitted: Submitted,
    *,
    grant_license: bool = False,
) -> list[dict[str, Any]]:
    """The smallest patch that makes the draft match the form.

    Unchanged fields produce no operation, so a resumed draft is not rewritten
    field by field on every deposit -- and the patch a test asserts on stays
    readable.
    """
    operations: list[dict[str, Any]] = []
    for (section_id, metadata_field), values in submitted.items():
        path = f"/sections/{section_id}/{metadata_field}"
        stored = stored_values(item, section_id, metadata_field)
        if values == stored:
            continue
        if values:
            operations.append({"op": "add", "path": path, "value": [{"value": v} for v in values]})
        else:
            operations.append({"op": "remove", "path": path})

    if grant_license and not _license_is_granted(item):
        # A string, not a boolean -- what DSpace's LicenseAddPatchOperation reads.
        operations.append({"op": "add", "path": LICENSE_PATH, "value": "true"})
    return operations


def _license_is_granted(item: WorkspaceItem) -> bool:
    section = item.sections.get(LICENSE_SECTION)
    return bool(section.get("granted")) if isinstance(section, dict) else False
