"""What a draft still needs before it can be deposited (Phase 4b).

The drafts list used to say what a draft *is* — title, collection, file, date —
and nothing about whether it could actually be submitted. The only way to find
out was to open each one. This turns the repository's own answer into a line the
user can read without leaving the list.

**DSpace is the authority here, exactly as it is for authorization.** Every
workspaceitem conversion runs each configured step's `validate()`
(`AInprogressItemConverter.fillFromModel`), so the `errors` array on a
`findBySubmitter` result is the same verdict the deposit call would give — not a
guess assembled here from the form config. This module only translates it:
`error.validation.required` on `/sections/traditionalpageone/dc.title` becomes
"Title — this field is required."

Labels come from the collection's live submission form config, so a collection
that calls `dc.title` something else says that. When the config cannot be read the
field's own name is shown instead: a draft that cannot be labelled still gets to
say what is wrong with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scholardrop.dspace.models import ValidationIssue, WorkspaceItem
from scholardrop.submission.form import message_for, parse_fields

# Error keys that mean "something has not been supplied yet", as opposed to
# "what you supplied is wrong". The difference is what the badge says, and it
# matters: "needs 3 fields" is a to-do, "2 problems" is a correction.
MISSING_KEYS = frozenset(
    {
        "error.validation.required",
        "error.validation.filerequired",
        "error.validation.license.notgranted",
    }
)

# Section-level paths carry no metadata field to label, so name the section.
SECTION_LABELS = {
    "upload": "File",
    "license": "Deposit licence",
    "collection": "Collection",
}

# How many blockers a row shows before it stops listing and starts counting.
SHOWN = 3


def _humanise(key: str) -> str:
    """A DSpace i18n key the review form has no wording for is still shown.

    `message_for` returns the key unchanged when it does not know it, which is
    right next to a form field (the reviewer can search for it) and wrong in a
    list. Anything still key-shaped becomes a plain sentence.
    """
    message = message_for(key)
    return "The repository is not happy with this yet." if message == key else message


@dataclass(frozen=True)
class Blocker:
    """One thing standing between a draft and a deposit."""

    label: str
    message: str
    #: True for "not supplied yet", False for "supplied but wrong".
    missing: bool = True

    @property
    def text(self) -> str:
        return f"{self.label} — {self.message}" if self.label else self.message


@dataclass(frozen=True)
class Readiness:
    """The verdict for one draft."""

    blockers: tuple[Blocker, ...] = ()
    #: False when the form config could not be read, so labels are raw field names.
    labelled: bool = True

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def missing_only(self) -> bool:
        return all(b.missing for b in self.blockers)

    @property
    def badge(self) -> str:
        if self.ready:
            return "Ready"
        count = len(self.blockers)
        if self.missing_only:
            return f"Needs {count}"
        return "1 problem" if count == 1 else f"{count} problems"

    @property
    def shown(self) -> tuple[Blocker, ...]:
        return self.blockers[:SHOWN]

    @property
    def hidden(self) -> int:
        return max(0, len(self.blockers) - SHOWN)


def labels_from(configs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Field label lookup, keyed both section-qualified and bare.

    Two sections can configure the same metadata field, so the qualified key wins
    where it exists; the bare one covers an error path pointing at a section the
    config does not describe.
    """
    labels: dict[str, str] = {}
    for section_id, config in configs.items():
        for field_config in parse_fields(config):
            if not field_config.label:
                continue
            labels[f"{section_id}/{field_config.metadata_field}"] = field_config.label
            labels.setdefault(field_config.metadata_field, field_config.label)
    return labels


def _label_for(path: str, labels: dict[str, str]) -> str:
    """Human name for an error path like 'traditionalpageone/dc.title'."""
    if path in labels:
        return labels[path]
    tail = path.rsplit("/", 1)[-1]
    if tail in labels:
        return labels[tail]
    if tail in SECTION_LABELS:
        return SECTION_LABELS[tail]
    # No configuration for it: the field's own name is a worse label than a real
    # one and a much better one than silence.
    return tail


def _blockers_of(issue: ValidationIssue, labels: dict[str, str]) -> list[Blocker]:
    message = _humanise(issue.message)
    missing = issue.message in MISSING_KEYS
    paths = issue.fields()
    if not paths:
        # An error DSpace did not attach to anything: still the user's problem.
        return [Blocker(label="", message=message, missing=missing)]
    return [Blocker(_label_for(path, labels), message, missing) for path in paths]


def assess(
    item: WorkspaceItem, labels: dict[str, str] | None = None, *, labelled: bool = True
) -> Readiness:
    """Turn a workspaceitem's `errors` into something a person can act on."""
    labels = labels or {}
    blockers: list[Blocker] = []
    seen: set[tuple[str, str]] = set()
    for issue in item.errors:
        for blocker in _blockers_of(issue, labels):
            key = (blocker.label, blocker.message)
            if key in seen:  # the same field failing twice reads as noise
                continue
            seen.add(key)
            blockers.append(blocker)
    # A draft with nothing wrong has nothing to mislabel, so it is never marked
    # as unlabelled.
    return Readiness(blockers=tuple(blockers), labelled=labelled or not blockers)
