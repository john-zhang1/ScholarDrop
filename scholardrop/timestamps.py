"""How ScholarDrop writes a repository timestamp for a person to read.

One implementation, because two pages now show them (the drafts list and the
submission status page) and two implementations would eventually disagree about
the timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime


def format_timestamp(raw: str | None) -> str:
    """A repository timestamp, precise enough to tell two of them apart.

    This used to be `raw[:10]` -- the date and nothing else -- so a dozen drafts
    started in one testing session all read "last changed 2026-06-04" and looked
    like the same thing saved repeatedly. They were a dozen separate submissions
    minutes apart. Truncation invented a pattern that was not in the data.

    DSpace sends UTC (`2026-07-28T06:37:16.538Z`). It is rendered as UTC and
    labelled as such: converting to the server's timezone would be a guess about
    where the reader is, and an unlabelled hour is worse than an honest one.
    """
    if not raw:
        return ""
    text = raw.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # An unparseable timestamp is still information; show what came back
        # rather than dropping it or pretending to a precision we do not have.
        return text
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def sort_key(raw: str | None) -> tuple[int, float, str]:
    """Newest-first ordering across sources that timestamp differently.

    The status page merges rows timestamped by DSpace (`lastModified`) with rows
    timestamped by this app (`deposited_at`), and one unparseable value must not
    reorder the rest: anything undateable sorts last, keeping its text order,
    rather than comparing as a string against real dates.
    """
    if not raw:
        return (0, 0.0, "")
    text = raw.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return (0, 0.0, text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (1, moment.timestamp(), text)
