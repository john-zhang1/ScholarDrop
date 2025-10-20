"""Who may read the file, and from when (DESIGN.md §13.5).

The repository decides what is on offer. A submission definition's upload section
links to its own configuration (`/config/submissionuploads/{id}`), and that
configuration lists the access conditions this collection allows -- typically
open access, an embargo, a lease. ScholarDrop renders exactly those and nothing
else: **no options configured means no picker**, because inventing one would
offer a promise about access that the repository would then refuse.

Each option says which dates it takes, and "takes" means *requires*
(`AccessConditionOption.validateResourcePolicy`, verified against 9.2): a
condition with `hasStartDate` must be given a start date and must not be given an
end date, and vice versa. `maxStartDate` / `maxEndDate` are the configured limits
already resolved to real dates by the server -- on this VM, embargo is +36 months
and lease +6 months.

**Which date it is decides what the words say.** A start date is when the public
policy *begins* (restricted until then); an end date is when it *stops* (open
until then). Naming either one "the embargo date" would be wrong half the time,
so the wording is derived from the option's shape rather than from its name.

The write happens once, at deposit, like everything else here:
`add /sections/{upload}/files/{n}/accessConditions` with the whole array replaces
that file's custom policies (`BitstreamResourcePolicyAddPatchOperation`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

log = logging.getLogger("scholardrop.submission")

# The form input carrying a file's chosen condition, and the one carrying its
# date. Indexed by the file's position in the upload section, which is the same
# position the patch path names.
NAME_PREFIX = "access"
DATE_SUFFIX = "date"

# DSpace's own shipped condition names, in words a submitter can act on. An
# unknown name is shown as it is: these are defaults, not this repository's
# vocabulary, and a name nobody recognised is better than a wrong translation.
CONDITION_LABELS = {
    "openaccess": "Open access",
    "embargo": "Embargoed",
    "lease": "Open for a limited time",
    "administrator": "Repository staff only",
}


@dataclass(frozen=True)
class AccessOption:
    """One access condition this collection's upload step offers."""

    name: str
    has_start_date: bool = False
    has_end_date: bool = False
    max_start_date: str = ""
    max_end_date: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AccessOption:
        return cls(
            name=str(payload.get("name") or ""),
            has_start_date=bool(payload.get("hasStartDate")),
            has_end_date=bool(payload.get("hasEndDate")),
            max_start_date=str(payload.get("maxStartDate") or ""),
            max_end_date=str(payload.get("maxEndDate") or ""),
        )

    @property
    def label(self) -> str:
        return CONDITION_LABELS.get(self.name, self.name)

    @property
    def needs_date(self) -> bool:
        return self.has_start_date or self.has_end_date

    @property
    def date_label(self) -> str:
        """What the date box is asking for, in the submitter's terms."""
        if self.has_start_date:
            return "Restricted until"
        if self.has_end_date:
            return "Open until"
        return ""

    @property
    def max_date(self) -> str:
        """The latest date this option accepts, as the repository computed it."""
        return self.max_start_date if self.has_start_date else self.max_end_date

    def describe(self, chosen: str = "") -> str:
        """This condition in a sentence, for the confirm and deposited pages."""
        if self.has_start_date and chosen:
            return f"{self.label} — restricted until {chosen}"
        if self.has_end_date and chosen:
            return f"{self.label} — open until {chosen}"
        return self.label

    def validate(self, chosen: str) -> str:
        """Why this date is not acceptable, or "" if it is.

        The same four rules DSpace applies, checked here so a submitter is told
        in the form rather than by a 422 at the end of the deposit. It is not a
        substitute for the repository's check -- the repository still decides.
        """
        if self.needs_date and not chosen:
            return f"{self.label} needs a date."
        if not self.needs_date and chosen:
            return f"{self.label} does not take a date."
        if not chosen:
            return ""
        try:
            given = date.fromisoformat(chosen)
        except ValueError:
            return "That date is not a date (use YYYY-MM-DD)."
        limit = self.max_date
        if limit:
            try:
                latest = date.fromisoformat(limit)
            except ValueError:  # a limit we cannot read is not one we can enforce
                return ""
            if given > latest:
                return f"{self.label} cannot go beyond {limit}."
        return ""

    def condition(self, chosen: str) -> dict[str, str]:
        """This choice as DSpace's `AccessConditionDTO`."""
        entry = {"name": self.name}
        if self.has_start_date and chosen:
            entry["startDate"] = chosen
        if self.has_end_date and chosen:
            entry["endDate"] = chosen
        return entry


@dataclass(frozen=True)
class AccessChoice:
    """What was chosen for one file: the option, and its date if it takes one."""

    index: int
    name: str = ""
    chosen_date: str = ""


@dataclass(frozen=True)
class AccessRow:
    """One file's picker, as the template renders it.

    The view model exists so the templates stay the dumb half: which option is
    selected, what its date box is called, and what is wrong with it are all
    decided here, where they can be tested without parsing HTML.
    """

    index: int
    filename: str
    options: list[AccessOption]
    chosen_name: str = ""
    chosen_date: str = ""
    error: str = ""

    @property
    def choice_name(self) -> str:
        return input_name(self.index)

    @property
    def date_name(self) -> str:
        return input_name(self.index, date_input=True)

    @property
    def selected(self) -> AccessOption | None:
        return option_named(self.options, self.chosen_name)

    @property
    def description(self) -> str:
        """This file's condition in a sentence, or "" when none was chosen."""
        option = self.selected
        return option.describe(self.chosen_date) if option else ""


def rows_for(
    files: list[Any],
    options: list[AccessOption],
    *,
    choices: dict[int, AccessChoice] | None = None,
    problems: dict[int, str] | None = None,
) -> list[AccessRow]:
    """A picker per uploaded file, preferring what was just posted.

    `choices` are the submitter's, and they win over what the draft stores --
    the same rule as `_apply_submitted` on the metadata form, and for the same
    reason: a page that comes back after a validation error must come back with
    the choices that were made, not with the ones the repository still holds.

    **No configured options means no rows**, and therefore no picker anywhere:
    the rule lives here rather than in each template that renders one.
    """
    if not options:
        return []
    choices = choices or {}
    problems = problems or {}
    rows = []
    for index, file in enumerate(files):
        choice = choices.get(index)
        rows.append(
            AccessRow(
                index=index,
                filename=getattr(file, "name", "") or "",
                options=options,
                chosen_name=choice.name if choice else getattr(file, "condition", ""),
                chosen_date=(choice.chosen_date if choice else getattr(file, "condition_date", "")),
                error=problems.get(index, ""),
            )
        )
    return rows


def parse_options(payload: dict[str, Any] | None) -> list[AccessOption]:
    """The conditions an upload configuration offers, in configured order."""
    entries = (payload or {}).get("accessConditionOptions") or []
    if not isinstance(entries, list):
        return []
    options = [AccessOption.from_payload(e) for e in entries if isinstance(e, dict)]
    return [o for o in options if o.name]


def option_named(options: list[AccessOption], name: str) -> AccessOption | None:
    return next((o for o in options if o.name == name), None)


def input_name(index: int, *, date_input: bool = False) -> str:
    suffix = f"::{DATE_SUFFIX}" if date_input else ""
    return f"{NAME_PREFIX}::{index}{suffix}"


def parse_choices(items: list[tuple[str, str]]) -> dict[int, AccessChoice]:
    """The picker's inputs, keyed by the file index they belong to.

    Anything that is not `access::<integer>` is ignored rather than guessed at:
    these names come back in a POST body, and the index goes straight into the
    patch path that names which file gets the policy.
    """
    chosen: dict[int, str] = {}
    dates: dict[int, str] = {}
    for name, raw in items:
        if not name.startswith(f"{NAME_PREFIX}::"):
            continue
        parts = name.split("::")
        if len(parts) not in (2, 3) or not parts[1].isascii() or not parts[1].isdigit():
            continue
        index = int(parts[1])
        if len(parts) == 3 and parts[2] == DATE_SUFFIX:
            dates[index] = str(raw).strip()
        elif len(parts) == 2:
            chosen[index] = str(raw).strip()
    return {
        index: AccessChoice(index=index, name=name, chosen_date=dates.get(index, ""))
        for index, name in chosen.items()
        if name
    }


def validate_choices(
    choices: dict[int, AccessChoice], options: list[AccessOption]
) -> dict[int, str]:
    """Per-file complaints, keyed by file index. Empty when everything is fine."""
    problems: dict[int, str] = {}
    for index, choice in sorted(choices.items()):
        option = option_named(options, choice.name)
        if option is None:
            # Not one of this collection's options: a stale page or a crafted
            # post. Refused here rather than sent for DSpace to refuse.
            problems[index] = "That is not an access condition this collection offers."
            continue
        problem = option.validate(choice.chosen_date)
        if problem:
            problems[index] = problem
    return problems


def build_operations(
    section_id: str,
    choices: dict[int, AccessChoice],
    options: list[AccessOption],
    *,
    file_count: int,
) -> list[dict[str, Any]]:
    """One `add` per file whose access condition was chosen.

    The whole array is sent for that file, which is what makes this a *set*:
    `BitstreamResourcePolicyAddPatchOperation` clears the file's custom policies
    first when the path ends at `accessConditions` (three segments), and appends
    when it ends at `/-`. Setting is the honest expression of a picker.

    A choice naming a file the draft does not have is dropped: the index is a
    position in the bundle, and one past the end would silently do nothing --
    or, worse, land on somebody else's file after a re-upload.
    """
    operations: list[dict[str, Any]] = []
    for index, choice in sorted(choices.items()):
        if not 0 <= index < file_count:
            log.info("ignoring an access condition for file %s, which this draft has no", index)
            continue
        option = option_named(options, choice.name)
        if option is None:
            log.info("ignoring unconfigured access condition %r", choice.name)
            continue
        operations.append(
            {
                "op": "add",
                "path": f"/sections/{section_id}/files/{index}/accessConditions",
                "value": [option.condition(choice.chosen_date)],
            }
        )
    return operations


def describe_choices(
    choices: dict[int, AccessChoice], options: list[AccessOption]
) -> dict[int, str]:
    """What each file's chosen condition says, for the confirm and deposited pages."""
    described: dict[int, str] = {}
    for index, choice in choices.items():
        option = option_named(options, choice.name)
        if option is not None:
            described[index] = option.describe(choice.chosen_date)
    return described
