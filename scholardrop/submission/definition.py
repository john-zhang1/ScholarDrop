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
import time
from typing import Any

from scholardrop.dspace import DSpaceError, VocabularyEntry
from scholardrop.dspace.models import WorkspaceItem
from scholardrop.submission.form import FORM_SECTION, parse_fields, sections_of

log = logging.getLogger("scholardrop.submission")


DEFAULT_TTL_SECONDS = 3600.0


class _Expiring:
    """A dict whose entries go stale, with just enough of the mapping protocol.

    Reads honour the TTL, so call sites keep using `get` / `in` / `[]` and simply
    stop seeing an entry once it is too old.
    """

    def __init__(self, ttl_seconds: float, *, clock: Any = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[Any, tuple[float, Any]] = {}

    def _fresh(self, key: Any) -> bool:
        stored = self._entries.get(key)
        if stored is None:
            return False
        if self._clock() - stored[0] > self.ttl_seconds:
            del self._entries[key]
            return False
        return True

    def __contains__(self, key: Any) -> bool:
        return self._fresh(key)

    def __getitem__(self, key: Any) -> Any:
        if not self._fresh(key):
            raise KeyError(key)
        return self._entries[key][1]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._entries[key] = (self._clock(), value)

    def get(self, key: Any, default: Any = None) -> Any:
        return self._entries[key][1] if self._fresh(key) else default

    def clear(self) -> None:
        self._entries.clear()

    def sweep(self) -> int:
        """Drop what has expired. Returns how many entries went."""
        before = len(self._entries)
        for key in list(self._entries):
            self._fresh(key)
        return before - len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


class ConfigCache:
    """Submission definitions, forms and vocabularies, keyed by instance.

    Entries expire. Repository configuration only changes when DSpace is
    reconfigured -- which restarts DSpace, not us -- so the TTL is long; but
    without one, a reconfigured collection served a stale form until someone
    thought to restart ScholarDrop, and the per-collection entries only ever grew.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self.definitions: Any = _Expiring(ttl_seconds)
        self.forms: Any = _Expiring(ttl_seconds)
        self.vocabularies: Any = _Expiring(ttl_seconds)
        # Keyed by collection, not by instance: the deposit licence is a
        # collection's own text.
        self.licences: Any = _Expiring(ttl_seconds)
        # Same again for the collection's display name, so naming the collection a
        # draft belongs to does not cost a read on every confirm and deposit.
        self.collection_names: Any = _Expiring(ttl_seconds)
        # Which live-import providers the instance has (DESIGN.md §13.1). Keyed by
        # instance, read once, and expiring like the rest -- a provider added to
        # DSpace's configuration should appear without restarting this app.
        self.external_sources: Any = _Expiring(ttl_seconds)

    def _all(self) -> tuple[Any, ...]:
        return (
            self.definitions,
            self.forms,
            self.vocabularies,
            self.licences,
            self.collection_names,
            self.external_sources,
        )

    def clear(self) -> None:
        for cache in self._all():
            cache.clear()

    def sweep(self) -> int:
        """Drop every expired entry, so nothing lingers unread. Returns how many."""
        return sum(cache.sweep() for cache in self._all())

    def __len__(self) -> int:
        return sum(len(cache) for cache in self._all())


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
