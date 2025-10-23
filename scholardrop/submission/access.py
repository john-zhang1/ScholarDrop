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
that file's custom policies (`BitstreamResourcePolicyAddPatchOperation`) -- and
an **empty** array therefore clears them, which is how an explicit "the
collection's default" steps a file back from a condition its draft stores.
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

# What choosing no condition means, in the words the confirm and deposited pages
# use. It is a real choice, distinct from open access: for a file that stores no
# custom condition it patches nothing, and for one that does it clears it.
DEFAULT_DESCRIPTION = "The collection's default"

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
    # Whether the submitter posted a choice for this file -- including the empty
    # one. "Chose the collection's default" and "was never asked" render the same
    # picker but must not deposit the same way: the first clears a stored
    # condition, the second leaves it alone.
    explicit: bool = False
    # The condition the draft already stores, so an explicit default over it can
    # be said in words rather than shown as silence.
    stored_condition: str = ""

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
        """This file's condition in a sentence, or "" when nothing will change."""
        option = self.selected
        if option:
            return option.describe(self.chosen_date)
        if self.explicit and not self.chosen_name and self.stored_condition:
            return DEFAULT_DESCRIPTION
        return ""


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
    **The empty choice wins too**: `choice is not None`, never truthiness, or
    picking "the collection's default" over a stored embargo would be silently
    put back to the embargo on the very next render.

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
        stored = getattr(file, "condition", "")
        rows.append(
            AccessRow(
                index=index,
                filename=getattr(file, "name", "") or "",
                options=options,
                chosen_name=choice.name if choice is not None else stored,
                chosen_date=(
                    choice.chosen_date
                    if choice is not None
                    else getattr(file, "condition_date", "")
                ),
                error=problems.get(index, ""),
                explicit=choice is not None,
                stored_condition=stored,
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

    **An empty value is kept, not dropped.** A posted `access::0=` is somebody
    choosing "the collection's default" -- a real decision, and the only way to
    step a file back from a condition its draft already stores. Dropping it made
    the choice unmakeable: the stored condition filled the gap on the next
    render and travelled to the deposit as though it had been chosen.
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
    }


def validate_choices(
    choices: dict[int, AccessChoice], options: list[AccessOption], *, file_count: int
) -> dict[int, str]:
    """Per-file complaints, keyed by file index. Empty when everything is fine.

    `file_count` is how many files the *item* holds at deposit time, and a
    choice naming a position past it is refused, not dropped: the page was
    rendered against files that are no longer there -- a re-upload in another
    tab, or a crafted post -- and silently ignoring the choice would let the
    confirmation claim a condition that was never sent.
    """
    problems: dict[int, str] = {}
    for index, choice in sorted(choices.items()):
        if not 0 <= index < file_count:
            problems[index] = "This draft no longer has that file."
            continue
        if not choice.name:
            # The collection's default is always applicable -- it asks the
            # repository for nothing the repository has not already decided.
            continue
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
    files: list[Any],
) -> list[dict[str, Any]]:
    """One `add` per file whose access condition was chosen.

    The whole array is sent for that file, which is what makes this a *set*:
    `BitstreamResourcePolicyAddPatchOperation` clears the file's custom policies
    first when the path ends at `accessConditions` (three segments), and appends
    when it ends at `/-`. Setting is the honest expression of a picker.

    **An explicit default clears.** For a file that stores no custom condition,
    choosing "the collection's default" is what not patching already means; for
    one that does, the empty array steps it back -- the operation removes the
    `TYPE_CUSTOM` policies before reading the array, and an empty array gives it
    nothing to re-apply (verified against the 9.2 source and live, 2026-07-31).

    A choice naming a file the draft does not have is dropped -- the index is a
    position in the bundle, and one past the end names nothing. `validate_choices`
    refuses such a choice before this runs; the guard here is for any other
    caller, and keeps the operations bounded by the same `files` the deposited
    page describes.
    """
    operations: list[dict[str, Any]] = []
    for index, choice in sorted(choices.items()):
        if not 0 <= index < len(files):
            log.info("ignoring an access condition for file %s, which this draft has no", index)
            continue
        if not choice.name:
            if getattr(files[index], "condition", ""):
                operations.append(
                    {
                        "op": "add",
                        "path": f"/sections/{section_id}/files/{index}/accessConditions",
                        "value": [],
                    }
                )
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
    choices: dict[int, AccessChoice], options: list[AccessOption], *, files: list[Any]
) -> dict[int, str]:
    """What each file's chosen condition says, for the confirm and deposited pages.

    Takes the same `files` as `build_operations` and applies the same bounds, so
    nothing can be described here that was not sent there -- the deposited page
    once claimed an embargo for a file index the patch had silently dropped,
    which is the one sentence this feature exists never to get wrong.
    """
    described: dict[int, str] = {}
    for index, choice in choices.items():
        if not 0 <= index < len(files):
            continue
        if not choice.name:
            if getattr(files[index], "condition", ""):
                described[index] = DEFAULT_DESCRIPTION
            continue
        option = option_named(options, choice.name)
        if option is not None:
            described[index] = option.describe(choice.chosen_date)
    return described
