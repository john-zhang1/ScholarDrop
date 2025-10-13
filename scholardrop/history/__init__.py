"""Where is my item? (DESIGN.md §13.3)

The repository can say what a submission *is* but not what this app *did*: a
deposited workspaceitem is consumed at the moment it succeeds, and nothing on
the DSpace side records that ScholarDrop sent it. So `log.py` keeps one row per
deposit, `status.py` reads live state back through the item uuid it saved, and
the two are merged with the drafts DSpace still holds.

Named `history` rather than `submissions` on purpose: `submission/` next door is
the machinery of *making* a submission, and two packages a letter apart would be
a permanent source of wrong imports.
"""

from scholardrop.history.log import Deposit, LogUnavailable, SubmissionLog, open_log
from scholardrop.history.status import (
    LABELS,
    State,
    Submission,
    counts,
    from_deposit,
    from_draft,
    merge,
    state_of,
)

__all__ = [
    "LABELS",
    "Deposit",
    "LogUnavailable",
    "State",
    "Submission",
    "SubmissionLog",
    "counts",
    "from_deposit",
    "from_draft",
    "merge",
    "open_log",
    "state_of",
]
