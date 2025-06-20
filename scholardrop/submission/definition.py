"""Fetching the live submission configuration a form is rendered from.

Rendering one form takes a handful of reads, because DSpace hands out its
configuration in layers (all verified against 9.2 on 2026-07-28):

* `embed=submissionDefinition` on the workspaceitem gives the definition **without
  its sections** -- just `{id, name, isDefault, _links}`. The panels come from
  `/config/submissiondefinitions/{id}?embed=sections`. (The definition's own
  `/sections` sub-resource answers 204, so following that link gets nothing.)
* each `submission-form` section only **links** to its field configuration;
* a `dropdown` field only names its controlled vocabulary.

They are cached per instance, because this is repository configuration: it is the
same for every user and only changes when DSpace is reconfigured (which restarts
DSpace). The cache lives on `app.state`, so restarting ScholarDrop clears it.
"""

from __future__ import annotations

import logging
from typing import Any

from scholardrop.dspace import DSpaceError, VocabularyEntry
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.submission.form import FORM_SECTION, parse_fields, sections_of

log = logging.getLogger("scholardrop.submission")


class ConfigCache:
    """Submission definitions, forms and vocabularies, keyed by instance."""

    def __init__(self) -> None:
        self.definitions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.forms: dict[tuple[str, str], dict[str, Any]] = {}
        self.vocabularies: dict[tuple[str, str], list[VocabularyEntry]] = {}
        # Keyed by collection, not by instance: the deposit licence is a
        # collection's own text.
        self.licences: dict[tuple[str, str], Any] = {}

    def clear(self) -> None:
        self.definitions.clear()
        self.forms.clear()
        self.vocabularies.clear()
        self.licences.clear()


async def load_sections(
    client: Any, item: WorkspaceItem, *, cache: ConfigCache, instance_key: str
) -> list[dict[str, Any]]:
    """The panels of this workspaceitem's submission definition, in order.

    Uses what the item embedded if a projection ever does embed the panels, and
    otherwise re-reads the definition by its self link -- which is the normal
    path against 9.2.
    """
    definition = item.submission_definition or {}
    embedded = sections_of(definition)
    if embedded:
        return embedded

    reference = (definition.get("_links") or {}).get("self", {}).get("href") or definition.get("id")
    if not reference:
        log.info("workspaceitem %s carries no submission definition", item.id)
        return []

    key = (instance_key, str(reference))
    sections = cache.definitions.get(key)
    if sections is None:
        sections = sections_of(await client.submission_definition(str(reference)))
        cache.definitions[key] = sections
    return sections


def config_hrefs(sections: list[dict[str, Any]]) -> dict[str, str]:
    """Section id -> the URL of its field configuration.

    Falls back to the section id, which is what the shipped `traditional`
    definition happens to use, but a custom definition may point a section at a
    differently-named form -- so the link is preferred.
    """
    hrefs: dict[str, str] = {}
    for panel in sections:
        section_id = str(panel.get("id"))
        if str(panel.get("sectionType") or "") != FORM_SECTION:
            continue
        href = (panel.get("_links") or {}).get("config", {}).get("href")
        hrefs[section_id] = str(href) if href else section_id
    return hrefs


async def load_form_configs(
    client: Any, sections: list[dict[str, Any]], *, cache: ConfigCache, instance_key: str
) -> dict[str, dict[str, Any]]:
    """Every `submission-form` section's field configuration, by section id."""
    configs: dict[str, dict[str, Any]] = {}
    for section_id, href in config_hrefs(sections).items():
        key = (instance_key, href)
        config = cache.forms.get(key)
        if config is None:
            config = await client.submission_form(href)
            cache.forms[key] = config
        configs[section_id] = config
    return configs


async def load_vocabularies(
    client: Any, configs: dict[str, dict[str, Any]], *, cache: ConfigCache, instance_key: str
) -> dict[str, list[VocabularyEntry]]:
    """Options for every controlled-vocabulary field in these forms.

    A vocabulary that cannot be read is not fatal: the field renders as a text
    box with a hint instead of a select, which is better than a form that will
    not load at all.
    """
    names = {
        field.vocabulary
        for config in configs.values()
        for field in parse_fields(config)
        if field.vocabulary
    }
    entries: dict[str, list[VocabularyEntry]] = {}
    for name in sorted(names):
        key = (instance_key, name)
        options = cache.vocabularies.get(key)
        if options is None:
            try:
                options = await client.all_vocabulary_entries(name)
            except DSpaceError as exc:
                log.warning("could not read vocabulary %s: %s", name, exc)
                options = []
            cache.vocabularies[key] = options
        entries[name] = options
    return entries
