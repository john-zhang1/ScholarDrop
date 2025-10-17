"""The review form, built from DSpace's own submission configuration.

Nothing here hardcodes a section name or a metadata field. The shape comes from
three live sources (DESIGN.md §3):

* the workspaceitem's **submission definition** -- which sections exist, in order;
* each `submission-form` section's **field configuration** (`/config/submissionforms/{id}`)
  -- labels, input types, mandatory/repeatable flags, selectable metadata;
* the workspaceitem's **section data** -- what is already stored.

Prefill is layered on top: the mapper's output is matched to whichever section
declares that metadata field. A collection with a custom SHAREOK ETD form works
without a code change, and an extracted value with nowhere to go is reported
rather than dropped.

Verified against DSpace 9.2's `traditional` definition: sections
collection / traditionalpageone / traditionalpagetwo / upload / license, with
input types onebox, textarea, date, dropdown, tag and series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholardrop.dspace.models import ValidationIssue, VocabularyEntry, WorkspaceItem
from scholardrop.extract.language import LANGUAGE_NAMES
from scholardrop.extract.mapper import MappedMetadata
from scholardrop.extract.models import TIER_LABELS, SourceTier

# Section types we render as an editable metadata form. Everything else
# (collection, upload, license, and any custom step) is shown, not edited.
FORM_SECTION = "submission-form"
# Appended to a field's input name for the hidden input holding its ORCID iD.
# `::` already separates section from field, so a third segment cannot collide
# with a metadata field name.
ORCID_SUFFIX = "::orcid"
# The same trick again, for the language a parallel value is written in (§13.4).
LANG_SUFFIX = "::lang"
UPLOAD_SECTION = "upload"
LICENSE_SECTION = "license"
COLLECTION_SECTION = "collection"

# DSpace ships i18n keys as section headers; the UI that resolves them is
# Angular's, not ours. Known keys get real English, anything else is prettified.
SECTION_HEADINGS = {
    "submit.progressbar.describe.stepone": "Describe your submission",
    "submit.progressbar.describe.steptwo": "Describe your submission (continued)",
    "submit.progressbar.upload": "File",
    "submit.progressbar.license": "Deposit licence",
    "submit.progressbar.CClicense": "Creative Commons licence",
    "submit.progressbar.accessCondition": "Access conditions",
    "submit.progressbar.collection": "Collection",
}

# Same treatment for the validation messages DSpace puts in `errors`.
ERROR_MESSAGES = {
    "error.validation.required": "This field is required.",
    "error.validation.license.notgranted": "The deposit licence has not been granted yet.",
    "error.validation.filerequired": "A file is required.",
}

# Input types that want a multi-line box, and those we must not free-text.
MULTILINE_INPUTS = frozenset({"textarea"})
CHOICE_INPUTS = frozenset({"dropdown"})


def heading_for(header: str | None, section_id: str) -> str:
    if not header:
        return section_id.replace("_", " ").capitalize()
    if header in SECTION_HEADINGS:
        return SECTION_HEADINGS[header]
    if "." in header:
        return header.rsplit(".", 1)[-1].replace("_", " ").capitalize()
    return header


def message_for(key: str) -> str:
    return ERROR_MESSAGES.get(key, key)


@dataclass(frozen=True)
class FieldConfig:
    """One input's configuration, as DSpace describes it."""

    metadata_field: str
    label: str
    input_type: str = "onebox"
    mandatory: bool = False
    repeatable: bool = False
    hint: str = ""
    mandatory_message: str = ""
    vocabulary: str | None = None
    closed: bool = False
    # True for the 2nd..nth selectable metadata of one configured field (the
    # "Identifiers" row offers six). They are rendered behind a disclosure so the
    # common case stays short.
    secondary: bool = False

    @property
    def multiline(self) -> bool:
        return self.input_type in MULTILINE_INPUTS

    @property
    def is_choice(self) -> bool:
        return self.input_type in CHOICE_INPUTS or bool(self.vocabulary)


def parse_fields(config: dict[str, Any]) -> list[FieldConfig]:
    """Flatten a `/config/submissionforms/{id}` document into fields.

    A configured field can offer several metadata fields (DSpace's "qualdrop"
    inputs, e.g. Identifiers -> ISSN / ISBN / URI ...). Each becomes its own
    input, because one input that rewrites its own name needs JavaScript we do
    not have; the extras are marked `secondary`.
    """
    fields: list[FieldConfig] = []
    for row in config.get("rows") or []:
        for raw in row.get("fields") or []:
            if not isinstance(raw, dict):
                continue
            selectable = [s for s in (raw.get("selectableMetadata") or []) if isinstance(s, dict)]
            label = str(raw.get("label") or "")
            input_type = str((raw.get("input") or {}).get("type") or "onebox")
            for position, option in enumerate(selectable):
                metadata_field = str(option.get("metadata") or "")
                if not metadata_field:
                    continue
                sublabel = option.get("label")
                fields.append(
                    FieldConfig(
                        metadata_field=metadata_field,
                        label=f"{label} — {sublabel}" if sublabel else label,
                        input_type=input_type,
                        mandatory=bool(raw.get("mandatory")) and position == 0,
                        repeatable=bool(raw.get("repeatable")),
                        hint=str(raw.get("hints") or "").strip(),
                        mandatory_message=str(raw.get("mandatoryMessage") or ""),
                        vocabulary=option.get("controlledVocabulary"),
                        closed=bool(option.get("closed")),
                        secondary=position > 0,
                    )
                )
    return fields


@dataclass
class FormValue:
    """One value of one field, and where it came from."""

    value: str = ""
    prefilled: bool = False
    low_confidence: bool = False
    # Which tier pre-filled it (DESIGN.md §13.1). Empty for a stored value or one
    # the user typed -- those need no explanation.
    source: SourceTier | None = None
    # An ORCID iD chosen for this value (DESIGN.md §13.2), carried through the
    # form whether or not the instance stores it anywhere.
    orcid: str = ""
    # The language *this value* is in (DESIGN.md §13.4), which is what makes two
    # rows of one field a work's title and its translation rather than a mistake.
    # Empty means the repository's default language, which is what an ordinary
    # single-language submission wants.
    language: str = ""

    @property
    def provenance(self) -> str:
        """How the page names this value's origin, or '' when it should not."""
        return TIER_LABELS.get(self.source, "") if self.prefilled and self.source else ""


@dataclass
class FormField:
    config: FieldConfig
    section_id: str
    values: list[FormValue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    options: list[VocabularyEntry] = field(default_factory=list)
    # True where this field holds a person and the instance offers the ORCID
    # type-ahead for it (DESIGN.md §13.2).
    orcid_lookup: bool = False
    # True where this field is bound to a controlled vocabulary that could not be
    # listed, so the box searches it instead of accepting anything (§13.4).
    vocabulary_lookup: bool = False
    # True where this field holds the work's own words, so each value can state
    # which language it is in and the field can gain a translation (§13.4).
    translatable: bool = False

    @property
    def name(self) -> str:
        """The form input name. Section-qualified because two sections can
        legitimately configure the same metadata field."""
        return f"{self.section_id}::{self.config.metadata_field}"

    @property
    def dom_id(self) -> str:
        return self.name.replace("::", "--").replace(".", "-")

    @property
    def filled(self) -> bool:
        return any(v.value for v in self.values)

    @property
    def prefilled(self) -> bool:
        return any(v.prefilled for v in self.values)

    @property
    def low_confidence(self) -> bool:
        return any(v.low_confidence and v.value for v in self.values)

    @property
    def required_empty(self) -> bool:
        """Highlighted in the form: DSpace will reject the deposit without it."""
        return self.config.mandatory and not self.filled

    @property
    def has_error(self) -> bool:
        """Anything the form is complaining about, from either source."""
        return bool(self.errors) or self.required_empty

    @property
    def describedby(self) -> str:
        """`aria-describedby` for this field's inputs: its hint and its error.

        Only ids that are actually rendered -- pointing at a missing element
        makes some screen readers announce nothing at all, which is worse than
        the silence it was meant to fix.
        """
        ids = []
        if self.config.hint:
            ids.append(f"hint-{self.dom_id}")
        if self.has_error:
            ids.append(f"error-{self.dom_id}")
        return " ".join(ids)

    @property
    def orcid_name(self) -> str:
        """The hidden input carrying this row's iD, parsed by `patch.parse_rows`."""
        return f"{self.name}{ORCID_SUFFIX}"

    @property
    def lang_name(self) -> str:
        """The input carrying this row's language, parsed the same way."""
        return f"{self.name}{LANG_SUFFIX}"

    @property
    def language_options(self) -> list[tuple[str, str]]:
        """(code, name) pairs for the translation picker, in name order."""
        return sorted(LANGUAGE_NAMES.items(), key=lambda pair: pair[1])

    def rows(self) -> list[FormValue]:
        """Always at least one row, so an empty field is still editable."""
        return self.values or [FormValue()]


@dataclass
class FormSection:
    id: str
    heading: str
    section_type: str
    fields: list[FormField] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Rendered, not edited: the upload section's files, the licence's state.
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def editable(self) -> bool:
        return self.section_type == FORM_SECTION

    @property
    def primary_fields(self) -> list[FormField]:
        return [f for f in self.fields if not f.config.secondary]

    @property
    def secondary_fields(self) -> list[FormField]:
        return [f for f in self.fields if f.config.secondary]

    @property
    def missing_required(self) -> list[FormField]:
        return [f for f in self.fields if f.required_empty]


@dataclass(frozen=True)
class UploadedFile:
    """A file already attached to the draft, as the upload section describes it.

    **There is no `name` key.** `UploadBitstreamRest` (verified against 9.2, and
    against a live draft on 2026-07-30) carries `uuid`, `metadata`, `format`,
    `sizeBytes`, `checkSum` and `url` -- the filename is a *metadata* value,
    `dc.title`, with `dc.source` holding the name it was uploaded under. Reading
    `file.name` therefore always found nothing; it only looked right because the
    page falls back to the filename this session remembers, which a resumed draft
    does not have.
    """

    name: str = ""
    size_bytes: int = 0
    uuid: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UploadedFile:
        metadata = payload.get("metadata") or {}

        def value_of(field: str) -> str:
            entries = metadata.get(field) or []
            first = entries[0] if isinstance(entries, list) and entries else None
            return str(first.get("value") or "") if isinstance(first, dict) else ""

        return cls(
            # `name` last, and only as a courtesy to anything that does send one.
            name=value_of("dc.title") or value_of("dc.source") or str(payload.get("name") or ""),
            size_bytes=int(payload.get("sizeBytes") or 0),
            uuid=str(payload.get("uuid") or ""),
        )

    @property
    def megabytes(self) -> float:
        return round(self.size_bytes / 1024 / 1024, 1)


@dataclass
class Unplaced:
    """An extracted value with nowhere to go, kept visible on purpose.

    The form shows these so the user can copy them into a field themselves --
    silently dropping something the extractor found is worse than admitting the
    form has no home for it.
    """

    label: str
    values: list[str]
    reason: str


@dataclass
class SubmissionForm:
    workspaceitem_id: str
    sections: list[FormSection] = field(default_factory=list)
    unplaced: list[Unplaced] = field(default_factory=list)

    def section(self, section_id: str) -> FormSection | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def field(self, section_id: str, metadata_field: str) -> FormField | None:
        section = self.section(section_id)
        if section is None:
            return None
        return next((f for f in section.fields if f.config.metadata_field == metadata_field), None)

    @property
    def editable_sections(self) -> list[FormSection]:
        return [s for s in self.sections if s.editable]

    @property
    def upload_files(self) -> list[UploadedFile]:
        section = next((s for s in self.sections if s.section_type == UPLOAD_SECTION), None)
        files = (section.data if section else {}).get("files") or []
        return [UploadedFile.from_payload(f) for f in files if isinstance(f, dict)]

    @property
    def license_granted(self) -> bool:
        section = next((s for s in self.sections if s.section_type == LICENSE_SECTION), None)
        return bool((section.data if section else {}).get("granted"))

    @property
    def prefilled_count(self) -> int:
        return sum(1 for s in self.sections for f in s.fields if f.prefilled)

    @property
    def missing_required(self) -> list[FormField]:
        return [f for s in self.sections for f in s.missing_required]


def sections_of(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """The panels of a submission definition fetched with `embed=sections`."""
    panels = (definition.get("_embedded") or {}).get("sections", {})
    if isinstance(panels, dict):
        panels = (panels.get("_embedded") or {}).get("sections", [])
    if not isinstance(panels, list):
        return []
    return [p for p in panels if isinstance(p, dict) and p.get("id")]


def defined_sections(
    item: WorkspaceItem, sections: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """The sections to render, in configured order.

    `sections` is what `submission.definition.load_sections` resolved, which is
    normally a second read: a workspaceitem embeds its definition *without* the
    panels (verified against 9.2). Failing that, this falls back to the sections
    the data carries -- unordered and with derived headings, but a form rather
    than a blank page.
    """
    if sections:
        return [p for p in sections if isinstance(p, dict) and p.get("id")]
    embedded = sections_of(item.submission_definition or {})
    if embedded:
        return embedded
    return [{"id": sid, "sectionType": _guess_type(sid)} for sid in item.section_ids]


def _guess_type(section_id: str) -> str:
    if section_id in (UPLOAD_SECTION, LICENSE_SECTION, COLLECTION_SECTION):
        return section_id
    return FORM_SECTION


def _values_of(section_data: Any, metadata_field: str) -> list[tuple[str, str, str]]:
    """Stored values of one metadata field, each with its authority and language.

    `[{"value": ..., "authority": ..., "language": ...}, ...]` ->
    `[(value, authority, language)]`. Both keys are carried for the same reason:
    a draft resumed on an ORCID-authority instance must show the iD it already
    holds, and a resumed bilingual draft must show which of its two titles is the
    translation -- otherwise re-depositing it flattens both into the default
    language, silently.
    """
    if not isinstance(section_data, dict):
        return []
    entries = section_data.get(metadata_field) or []
    if not isinstance(entries, list):
        return []
    out: list[tuple[str, str, str]] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("value"):
            out.append(
                (
                    str(entry["value"]),
                    str(entry.get("authority") or ""),
                    str(entry.get("language") or ""),
                )
            )
        elif isinstance(entry, str) and entry:
            out.append((entry, "", ""))
    return out


def _errors_by_path(errors: list[ValidationIssue]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for issue in errors:
        for path in issue.fields():
            out.setdefault(path, []).append(message_for(issue.message))
    return out


def build_form(
    item: WorkspaceItem,
    form_configs: dict[str, dict[str, Any]],
    *,
    sections: list[dict[str, Any]] | None = None,
    mapped: MappedMetadata | None = None,
    vocabularies: dict[str, list[VocabularyEntry]] | None = None,
    submitted: dict[tuple[str, str], list[str]] | None = None,
    submitted_orcids: dict[tuple[str, str], list[str]] | None = None,
    submitted_languages: dict[tuple[str, str], list[str]] | None = None,
    extra_errors: dict[str, list[str]] | None = None,
    orcid_policy: Any = None,
    vocabulary_typeahead: bool = False,
    translatable_fields: frozenset[str] = frozenset(),
) -> SubmissionForm:
    """Assemble the review form.

    `sections` is the resolved submission definition; `form_configs` is keyed by
    section id (only `submission-form` sections have one). `mapped` is the
    extractor's output already translated into metadata fields; it fills only
    fields that are still empty, so resuming a draft never loses what the user
    typed.

    `submitted` overrides the stored values with what the user just posted, and
    `extra_errors` adds messages DSpace returned at deposit time -- together they
    are how a rejected deposit comes back as the same form with the same edits
    and the problems marked, rather than as a dead end.
    """
    vocabularies = vocabularies or {}
    errors = _errors_by_path(item.errors)
    form = SubmissionForm(workspaceitem_id=item.id)

    for panel in defined_sections(item, sections):
        section_id = str(panel.get("id"))
        section_type = str(panel.get("sectionType") or _guess_type(section_id))
        section = FormSection(
            id=section_id,
            heading=heading_for(panel.get("header"), section_id),
            section_type=section_type,
            data=item.sections.get(section_id) or {},
            errors=errors.get(section_id, []),
        )
        if section_type == FORM_SECTION:
            data = item.sections.get(section_id)
            for config in parse_fields(form_configs.get(section_id) or {}):
                values = [
                    FormValue(value=value, orcid=authority, language=language)
                    for value, authority, language in _values_of(data, config.metadata_field)
                ]
                options = vocabularies.get(config.vocabulary or "", [])
                orcid = bool(orcid_policy and orcid_policy.offers_lookup(config.metadata_field))
                section.fields.append(
                    FormField(
                        config=config,
                        section_id=section_id,
                        values=values,
                        errors=errors.get(f"{section_id}/{config.metadata_field}", []),
                        options=options,
                        orcid_lookup=orcid,
                        # Only where the options could not be listed: a vocabulary
                        # that *did* answer is already a <select>, which is better
                        # than a search box. And never on a field that already has
                        # the ORCID type-ahead -- one input can carry one hx-get,
                        # and a person's name is not a vocabulary term.
                        vocabulary_lookup=bool(
                            vocabulary_typeahead and config.vocabulary and not options and not orcid
                        ),
                        translatable=config.metadata_field in translatable_fields,
                    )
                )
        form.sections.append(section)

    if mapped is not None:
        _apply_prefill(form, mapped)
    if submitted is not None:
        _apply_submitted(form, submitted, submitted_orcids or {}, submitted_languages or {})
    if extra_errors:
        _apply_errors(form, extra_errors)
    return form


def _apply_submitted(
    form: SubmissionForm,
    submitted: dict[tuple[str, str], list[str]],
    orcids: dict[tuple[str, str], list[str]] | None = None,
    languages: dict[tuple[str, str], list[str]] | None = None,
) -> None:
    """Re-render with what the user just typed, not with what DSpace stores.

    Used when the form comes back -- the confirm step, a validation failure, or
    "back to editing" -- so an edit is never silently lost between requests. The
    chosen iDs and the chosen languages travel with their values, or picking one
    and then hitting a validation error would quietly discard it.
    """
    orcids = orcids or {}
    languages = languages or {}
    for section in form.editable_sections:
        for field_ in section.fields:
            key = (section.id, field_.config.metadata_field)
            values = submitted.get(key)
            if values is None:
                continue
            chosen = orcids.get(key) or []
            tagged = languages.get(key) or []
            field_.values = [
                FormValue(
                    value=v,
                    orcid=chosen[i] if i < len(chosen) else "",
                    language=tagged[i] if i < len(tagged) else "",
                )
                for i, v in enumerate(values)
            ]


def _apply_errors(form: SubmissionForm, errors: dict[str, list[str]]) -> None:
    """Add messages keyed by `section/metadata.field` (or a bare section id)."""
    for section in form.sections:
        section.errors = section.errors + errors.get(section.id, [])
        for field_ in section.fields:
            extra = errors.get(f"{section.id}/{field_.config.metadata_field}", [])
            field_.errors = field_.errors + extra


def _apply_prefill(form: SubmissionForm, mapped: MappedMetadata) -> None:
    """Drop extracted values into the fields that accept them.

    Rules, in order of how often they bite:
    * a field that already holds a value is left alone (resume must not clobber);
    * a non-repeatable field takes the first value, and the rest are reported;
    * a metadata field no section declares is reported, never silently dropped.
    """
    index: dict[str, FormField] = {}
    for section in form.editable_sections:
        for field_ in section.fields:
            index.setdefault(field_.config.metadata_field, field_)

    for metadata_field, values in mapped.fields.items():
        low = mapped.low_confidence.get(metadata_field, False)
        target = index.get(metadata_field)
        if target is None:
            form.unplaced.append(
                Unplaced(
                    label=metadata_field,
                    values=values,
                    reason="this collection's form has no field for it",
                )
            )
            continue
        if target.filled:
            form.unplaced.append(
                Unplaced(
                    label=f"{target.config.label} ({metadata_field})",
                    values=values,
                    reason="the field already has a value",
                )
            )
            continue
        accepted = values if target.config.repeatable else values[:1]
        source = mapped.sources.get(metadata_field)
        target.values = [
            FormValue(value=v, prefilled=True, low_confidence=low, source=source) for v in accepted
        ]
        if len(accepted) < len(values):
            form.unplaced.append(
                Unplaced(
                    label=f"{target.config.label} ({metadata_field})",
                    values=values[len(accepted) :],
                    reason="the field takes only one value",
                )
            )

    for name, values in mapped.unmapped.items():
        form.unplaced.append(
            Unplaced(
                label=name.replace("_", " "),
                values=values,
                reason="not mapped to a metadata field for this repository",
            )
        )
