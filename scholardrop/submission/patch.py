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
from typing import Any, NamedTuple

from scholardrop.dspace.models import WorkspaceItem
from scholardrop.extract.language import LANGUAGE_NAMES
from scholardrop.orcid import CF_ACCEPTED, OrcidPolicy, is_orcid
from scholardrop.submission.form import (
    FORM_SECTION,
    LANG_SUFFIX,
    LICENSE_SECTION,
    ORCID_SUFFIX,
    parse_fields,
)

log = logging.getLogger("scholardrop.submission")

NAME_SEPARATOR = "::"
LICENSE_FIELD = "license_granted"
LICENSE_PATH = f"/sections/{LICENSE_SECTION}/granted"

# Submitted values, keyed by (section id, metadata field).
Submitted = dict[tuple[str, str], list[str]]
# ORCID iDs, aligned with the values of the same key.
Orcids = dict[tuple[str, str], list[str]]
# Language codes, aligned the same way (DESIGN.md §13.4).
Languages = dict[tuple[str, str], list[str]]


class Row(NamedTuple):
    """One row of one field: what was typed, and what was chosen alongside it."""

    value: str
    orcid: str = ""
    language: str = ""


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


def parse_rows(
    items: list[tuple[str, str]], allowed: dict[tuple[str, str], Any]
) -> dict[tuple[str, str], list[Row]]:
    """Read the form's inputs as rows: a value, and what was chosen alongside it.

    Every row renders its value input immediately followed by its hidden
    `…::orcid` and `…::lang`, so pairing is by document order rather than by
    index -- an empty row contributes no value, and an index-based pairing would
    then hand its iD (or its language) to the next row down the list.

    `items` is the form's multi-items, so a repeatable field arrives as several
    pairs with the same name and their order is the order the user sees.
    """
    rows: dict[tuple[str, str], list[Row]] = {}
    for name, raw in items:
        if NAME_SEPARATOR not in name:
            continue
        companion = next((s for s in (ORCID_SUFFIX, LANG_SUFFIX) if name.endswith(s)), "")
        base = name[: -len(companion)] if companion else name
        section_id, _, metadata_field = base.partition(NAME_SEPARATOR)
        key = (section_id, metadata_field)
        if key not in allowed:
            if not companion:
                log.info("ignoring submitted value for unconfigured field %s", name)
            continue
        value = str(raw).strip()
        if companion:
            # Belongs to the row just read. A stray one with no row before it is
            # a crafted post, and has nothing to attach to.
            if rows.get(key):
                row = rows[key][-1]
                rows[key][-1] = (
                    row._replace(orcid=value)
                    if companion == ORCID_SUFFIX
                    else row._replace(language=value)
                )
            continue
        rows.setdefault(key, []).append(Row(value))
    return rows


def parse_submission(
    items: list[tuple[str, str]], allowed: dict[tuple[str, str], Any]
) -> Submitted:
    """The submitted values, empty rows dropped, keyed by (section, field)."""
    return {
        key: [row.value for row in rows if row.value]
        for key, rows in parse_rows(items, allowed).items()
    }


def parse_orcids(
    items: list[tuple[str, str]], allowed: dict[tuple[str, str], Any]
) -> dict[tuple[str, str], list[str]]:
    """The iDs, positionally aligned with `parse_submission`'s values.

    Only rows that kept a value are represented, so index *i* here is the iD of
    value *i* there -- which is what makes an authority key land on the right
    name.
    """
    aligned: dict[tuple[str, str], list[str]] = {}
    for key, rows in parse_rows(items, allowed).items():
        kept = [row for row in rows if row.value]
        chosen = [row.orcid if is_orcid(row.orcid) else "" for row in kept]
        if any(chosen):
            aligned[key] = chosen
    return aligned


def parse_languages(items: list[tuple[str, str]], allowed: dict[tuple[str, str], Any]) -> Languages:
    """The languages, positionally aligned with `parse_submission`'s values.

    Only codes the picker offers are kept: the language key ends up in the
    repository's index, and an unrecognised one posted by hand would sit there
    for ever. An empty code means the repository's default language, which is
    what an ordinary submission wants and what most rows carry.
    """
    aligned: Languages = {}
    for key, rows in parse_rows(items, allowed).items():
        kept = [row for row in rows if row.value]
        chosen = [row.language if row.language in LANGUAGE_NAMES else "" for row in kept]
        if any(chosen):
            aligned[key] = chosen
    return aligned


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
    orcids: Orcids | None = None,
    languages: Languages | None = None,
    policy: OrcidPolicy | None = None,
) -> list[dict[str, Any]]:
    """The smallest patch that makes the draft match the form.

    Unchanged fields produce no operation, so a resumed draft is not rewritten
    field by field on every deposit -- and the patch a test asserts on stays
    readable.

    Chosen ORCID iDs are written the way `policy` says and no other way
    (DESIGN.md §13.2): as an `authority` key on the value, into a parallel field,
    or nowhere at all. `display_only` is the default because the wrong choice
    here writes a dangling authority key into a live index.
    """
    orcids = orcids or {}
    languages = languages or {}
    operations: list[dict[str, Any]] = []
    for (section_id, metadata_field), values in submitted.items():
        path = f"/sections/{section_id}/{metadata_field}"
        stored = stored_values(item, section_id, metadata_field)
        chosen = orcids.get((section_id, metadata_field), [])
        tagged = languages.get((section_id, metadata_field), [])
        authoritative = bool(policy and policy.stores_authority and any(chosen))
        # Same rule as the authority key: the stored comparison is of values
        # alone, so a field whose *text* is unchanged but whose language has been
        # set would otherwise produce no operation and lose the choice.
        if values == stored and not authoritative and not any(tagged):
            continue
        if values:
            operations.append(
                {
                    "op": "add",
                    "path": path,
                    "value": [
                        _value_object(
                            v,
                            chosen[i] if i < len(chosen) else "",
                            authoritative,
                            tagged[i] if i < len(tagged) else "",
                        )
                        for i, v in enumerate(values)
                    ],
                }
            )
        else:
            operations.append({"op": "remove", "path": path})

    operations.extend(_orcid_field_operations(item, submitted, orcids, policy))

    if grant_license and not _license_is_granted(item):
        # A string, not a boolean -- what DSpace's LicenseAddPatchOperation reads.
        operations.append({"op": "add", "path": LICENSE_PATH, "value": "true"})
    return operations


def _value_object(
    value: str, orcid: str, authoritative: bool, language: str = ""
) -> dict[str, Any]:
    """One entry of a metadata patch value: the text, and what qualifies it.

    `confidence` accompanies `authority` always: DSpace stores the pair, and a
    value with an authority key and no confidence is indexed at "unset", which
    excludes it from the authority-controlled browse the key exists to feed.

    `language` is `MetadataValueRest.language` -- the same key DSpace's own UI
    writes, which is what makes two values of `dc.title` a title and its
    translation rather than two titles (DESIGN.md §13.4).
    """
    entry: dict[str, Any] = {"value": value}
    if authoritative and orcid:
        entry["authority"] = orcid
        entry["confidence"] = CF_ACCEPTED
    if language:
        entry["language"] = language
    return entry


def _orcid_field_operations(
    item: WorkspaceItem,
    submitted: Submitted,
    orcids: Orcids,
    policy: OrcidPolicy | None,
) -> list[dict[str, Any]]:
    """The parallel-field write, in `field:` mode.

    Which iDs go there depends on what the *form* says the field is. A repeatable
    target takes the primary author field's iDs in order; a single-valued one
    takes the first, because that is what "the author's ORCID iD" means on a form
    that offers exactly one box for it. Nothing is invented: a target that the
    collection does not configure was already resolved to `unavailable` and is
    reported on the page instead.
    """
    if policy is None or not policy.stores_field or policy.target is None:
        return []
    section_id, target_field = policy.target
    ids = [
        orcid
        for (_, metadata_field), chosen in orcids.items()
        if metadata_field == policy.primary_field
        for orcid in chosen
        if orcid
    ]
    if not ids:
        return []
    if not policy.target_repeatable:
        ids = ids[:1]

    path = f"/sections/{section_id}/{target_field}"
    if ids == stored_values(item, section_id, target_field):
        return []
    return [{"op": "add", "path": path, "value": [{"value": i} for i in ids]}]


def _license_is_granted(item: WorkspaceItem) -> bool:
    section = item.sections.get(LICENSE_SECTION)
    return bool(section.get("granted")) if isinstance(section, dict) else False
