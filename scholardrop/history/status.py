"""Where is my item? (DESIGN.md §13.3)

Two sources answer that, and neither answers it alone. `findBySubmitter` knows
every draft the repository is holding but nothing about what has already been
deposited; the local log (`history/log.py`) knows every deposit *this app* made
but nothing about what happened to it since. This module is the join: state
derivation from a live lookup, and the merge that turns two lists into one.

Everything here is pure. The route does the I/O, so the interesting part -- a
matrix of log row against live lookup -- is testable without a repository.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from scholardrop.dspace.models import ItemStatus
from scholardrop.history.log import Deposit
from scholardrop.submission.drafts import Draft
from scholardrop.timestamps import format_timestamp, sort_key


class State(StrEnum):
    """What has become of one submission.

    DESIGN.md names three; there are five, and the two extra ones are there
    because folding them into the others would print something untrue.
    `WITHDRAWN` is not `LIVE` -- the item is in the archive but deliberately not
    public, and telling its submitter it is available would be wrong. `UNKNOWN`
    is not "in progress": the repository did not say, and a page that guesses in
    that situation is worse than one that admits it.
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    LIVE = "live"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"


LABELS = {
    State.DRAFT: "Draft",
    State.IN_REVIEW: "In review",
    State.LIVE: "Live",
    State.WITHDRAWN: "Withdrawn",
    State.UNKNOWN: "Unknown",
}

#: Newest first *within* a state would be wrong -- the page is a timeline, not a
#: queue -- so this only orders rows that share a timestamp.
_TIEBREAK = {
    State.DRAFT: 0,
    State.IN_REVIEW: 1,
    State.LIVE: 2,
    State.WITHDRAWN: 3,
    State.UNKNOWN: 4,
}


@dataclass
class Submission:
    """One row of the status page."""

    state: State
    title: str = ""
    collection_name: str = ""
    when: str = ""
    when_iso: str = ""
    #: Present only while the submission is a draft; it is what "Resume" needs.
    #: Deliberately *not* the id in the log -- that one names a workspaceitem the
    #: deposit consumed, and offering it as a link would 404.
    wsi_id: str = ""
    #: The id the log recorded. Kept because a rejection mints a new
    #: workspaceitem, so the merge has to recognise a submission by either id.
    #: Never rendered.
    logged_wsi_id: str = ""
    #: Only ever a *deposit's* item uuid, resolved before the deposit consumed
    #: the draft. A live draft has none (see `WorkspaceItem`).
    item_uuid: str = ""
    handle: str = ""
    workflow_id: str = ""
    #: Why the state is what it is, when that needs saying. Only ever set for
    #: UNKNOWN -- the other four states speak for themselves.
    detail: str = ""
    readiness: Any = None
    filenames: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.state]

    @property
    def is_draft(self) -> bool:
        return self.state is State.DRAFT

    @property
    def handle_url(self) -> str:
        return f"https://hdl.handle.net/{self.handle}" if self.handle else ""

    def as_draft(self) -> Draft:
        """This row as the drafts list would render it, for `draft_actions.html`.

        The partial takes a `Draft`; rather than teach it a second shape, a draft
        row hands back the one it came from.
        """
        return Draft(
            id=self.wsi_id,
            title=self.title,
            collection_name=self.collection_name,
            last_modified=self.when,
            last_modified_iso=self.when_iso,
            filenames=list(self.filenames),
            readiness=self.readiness,
        )


def state_of(status: ItemStatus) -> tuple[State, str]:
    """What a live lookup means, and what to say about it if it means nothing.

    Order matters, and the live check at the Phase 10 gate showed it matters more
    than expected: withdrawing an item sets `withdrawn` true and **`inArchive`
    back to false**. Read in the wrong order that is not merely "live" instead of
    "withdrawn" -- it falls past the archive test, finds no workflowitem, and
    reports a pulled item to its submitter as an editable draft. `withdrawn` is
    therefore asked first and answers on its own.

    "Not archived, not withdrawn, not in a workflow" really is a draft: that is
    what a rejected submission becomes.
    """
    if not status.checked:
        return State.UNKNOWN, "Its current state could not be checked just now."
    if not status.found:
        return State.UNKNOWN, "The repository no longer has this item."
    if status.withdrawn:
        return State.WITHDRAWN, ""
    if status.in_archive:
        return State.LIVE, ""
    if status.in_workflow:
        return State.IN_REVIEW, ""
    return State.DRAFT, ""


def from_deposit(deposit: Deposit, status: ItemStatus) -> Submission:
    """One logged deposit, as it stands now.

    The *live* answer wins over the logged one wherever they overlap: a handle
    the log never saw (the deposit entered review and was approved later) and a
    title that has been edited since both come from the repository. The log
    supplies what only it knows -- when this app sent it, and to where.
    """
    state, detail = state_of(status)
    return Submission(
        state=state,
        title=deposit.title,
        collection_name=deposit.collection_name,
        when=format_timestamp(deposit.deposited_at),
        when_iso=deposit.deposited_at,
        wsi_id=status.workspaceitem_id,
        logged_wsi_id=deposit.wsi_id,
        item_uuid=deposit.item_uuid or status.uuid,
        handle=status.handle or deposit.handle,
        workflow_id=status.workflow_id or deposit.workflow_id,
        detail=detail,
    )


def from_draft(draft: Draft) -> Submission:
    """A draft the repository is holding, which by definition is in progress."""
    return Submission(
        state=State.DRAFT,
        title=draft.title,
        collection_name=draft.collection_name,
        when=draft.last_modified,
        when_iso=draft.last_modified_iso,
        wsi_id=draft.id,
        readiness=draft.readiness,
        filenames=list(draft.filenames),
    )


def merge(
    deposits: Iterable[Submission], drafts: Sequence[Submission], *, limit: int = 25
) -> list[Submission]:
    """One list, newest first, with each submission appearing once.

    Neither input has to arrive in any order: both are sorted before the quota
    is applied, because a caller handing them over oldest-first would otherwise
    silently get the *oldest* rows kept -- a wrong page with nothing to show for
    it.

    **Drafts win.** A deposit that came back out of review is the same submission
    as one of the live drafts, and the live row is the better one: it carries the
    repository's own readiness verdict and the id that "Resume" needs. So a
    logged deposit is dropped when a draft already represents it.

    Matching is by **workspaceitem id, and it takes two of them**. A rejection
    mints a *new* workspaceitem, so the id in the log names something that no
    longer exists; the live lookup supplies the current one (`wsi_id`) and the
    log supplies the original (`logged_wsi_id`), and a draft matching either is
    the same submission. Matching on the item uuid would be the obvious join and
    is not available: a workspaceitem does not carry one (see
    `WorkspaceItem`), and buying it would cost a request per draft.

    **Deposits are never crowded out.** Found at the Phase 10 gate: an account
    with eighty abandoned drafts, every one of them touched more recently than
    any deposit, filled the whole page -- so the one thing somebody opens this
    page to find was the one thing not on it. Trimming is therefore not "the
    newest N of everything": every deposit gets a place first and drafts fill
    what is left. The rows are still *displayed* newest-first, so it still reads
    as a timeline.
    """

    def newest_first(rows: Iterable[Submission]) -> list[Submission]:
        return sorted(
            rows, key=lambda row: (sort_key(row.when_iso), -_TIEBREAK[row.state]), reverse=True
        )

    seen_drafts = {row.wsi_id for row in drafts if row.wsi_id}
    kept = newest_first(
        row for row in deposits if not seen_drafts & ({row.wsi_id, row.logged_wsi_id} - {""})
    )
    remaining = newest_first(drafts)
    if limit:
        kept = kept[:limit]
        remaining = remaining[: max(limit - len(kept), 0)]
    return newest_first(kept + remaining)


def counts(rows: Sequence[Submission]) -> dict[State, int]:
    """How many rows are in each state, for the summary line."""
    tally = dict.fromkeys(State, 0)
    for row in rows:
        tally[row.state] += 1
    return tally
