"""State derivation and the merge (DESIGN.md §13.3).

The matrix the phase asks for: one logged deposit against every shape of live
lookup, then the join with the drafts DSpace still holds.
"""

from __future__ import annotations

import pytest

from scholardrop.dspace.models import ItemStatus
from scholardrop.history.log import Deposit
from scholardrop.history.status import (
    State,
    Submission,
    counts,
    from_deposit,
    from_draft,
    merge,
    state_of,
)
from scholardrop.submission.drafts import Draft


def a_deposit(**overrides: object) -> Deposit:
    fields: dict = {
        "base_url": "https://dspace.test/server",
        "instance_key": "test",
        "eperson_uuid": "eperson-1",
        "wsi_id": "42",
        "item_uuid": "item-1",
        "title": "A Thesis",
        "collection_name": "OU - Theses",
        "deposited_at": "2026-07-30T10:00:00+00:00",
    }
    fields.update(overrides)
    return Deposit(**fields)  # type: ignore[arg-type]


def a_draft(**overrides: object) -> Draft:
    fields: dict = {
        "id": "7",
        "title": "A Draft",
        "collection_name": "OU - Theses",
        "last_modified": "2026-07-29 09:00 UTC",
        "filenames": ["thesis.pdf"],
        "last_modified_iso": "2026-07-29T09:00:00Z",
    }
    fields.update(overrides)
    return Draft(**fields)  # type: ignore[arg-type]


# ---- the state matrix ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "status", "expected"),
    [
        (
            "archived",
            ItemStatus(uuid="i", found=True, in_archive=True, handle="11244/1"),
            State.LIVE,
        ),
        ("in review", ItemStatus(uuid="i", found=True, in_workflow=True), State.IN_REVIEW),
        ("back with its submitter", ItemStatus(uuid="i", found=True), State.DRAFT),
        (
            "withdrawn",
            ItemStatus(uuid="i", found=True, in_archive=True, withdrawn=True),
            State.WITHDRAWN,
        ),
        ("removed from the repository", ItemStatus.gone("i"), State.UNKNOWN),
        ("could not be asked", ItemStatus.unchecked("i"), State.UNKNOWN),
    ],
)
def test_every_live_lookup_maps_to_one_state(
    name: str, status: ItemStatus, expected: State
) -> None:
    assert state_of(status)[0] is expected, name


def test_a_withdrawn_item_is_not_reported_as_live_or_as_a_draft() -> None:
    """Withdrawing an item sets `inArchive` back to *false* (checked live at the
    Phase 10 gate), so the wrong order does not merely say "live" -- it falls
    through to the draft branch and offers its submitter a Resume button for an
    item the repository has pulled."""
    for in_archive in (True, False):
        withdrawn = ItemStatus(uuid="i", found=True, in_archive=in_archive, withdrawn=True)
        assert state_of(withdrawn)[0] is State.WITHDRAWN


def test_unknown_says_which_kind_of_unknown_it_is() -> None:
    """ "Removed" and "we could not ask" must never render as the same sentence."""
    gone = state_of(ItemStatus.gone("i"))[1]
    unchecked = state_of(ItemStatus.unchecked("i"))[1]
    assert gone and unchecked and gone != unchecked
    assert "no longer" in gone
    assert "could not be checked" in unchecked


# ---- a logged deposit, rendered ------------------------------------------


def test_the_live_handle_wins_over_the_logged_one() -> None:
    """A deposit that entered review has no handle when it is logged; the one it
    gains at approval is only ever visible live."""
    row = from_deposit(
        a_deposit(handle=""),
        ItemStatus(uuid="item-1", found=True, in_archive=True, handle="11244/345333"),
    )
    assert row.state is State.LIVE
    assert row.handle == "11244/345333"
    assert row.handle_url == "https://hdl.handle.net/11244/345333"


def test_a_rejected_submission_carries_the_draft_it_became() -> None:
    """The id in the log names a workspaceitem the deposit consumed. Without the
    new one, "Resume" on a returned submission would point at nothing."""
    row = from_deposit(
        a_deposit(wsi_id="42"),
        ItemStatus(uuid="item-1", found=True, workspaceitem_id="99"),
    )
    assert row.state is State.DRAFT
    assert row.wsi_id == "99"


def test_a_row_keeps_the_deposit_time_the_repository_cannot_know() -> None:
    row = from_deposit(a_deposit(), ItemStatus(uuid="item-1", found=True, in_archive=True))
    assert row.when_iso == "2026-07-30T10:00:00+00:00"
    assert row.when == "2026-07-30 10:00 UTC"


def test_an_unreadable_row_still_renders_its_title() -> None:
    row = from_deposit(a_deposit(), ItemStatus.unchecked("item-1"))
    assert row.state is State.UNKNOWN
    assert row.title == "A Thesis"
    assert row.detail


# ---- the merge -----------------------------------------------------------


def test_a_deposit_that_came_back_is_not_listed_twice() -> None:
    """Same submission, two sources. The live draft is the better row.

    A rejection mints a *new* workspaceitem, so the id in the log (42) is not the
    id of the draft it became (7). The live lookup is what supplies the new one,
    and it is the only join available -- a workspaceitem carries no item uuid.
    """
    returned = from_deposit(
        a_deposit(wsi_id="42", item_uuid="item-7"),
        ItemStatus(uuid="item-7", found=True, workspaceitem_id="7"),
    )
    rows = merge([returned], [from_draft(a_draft(id="7"))])
    assert len(rows) == 1
    assert rows[0].readiness is not None  # the draft's row, which carries the verdict
    assert rows[0].wsi_id == "7"


def test_a_deposit_still_matches_the_draft_id_it_was_logged_under() -> None:
    """A draft that was never rejected -- a retried request, say -- has the same
    id in both places, and must still be one row."""
    orphan = from_deposit(a_deposit(wsi_id="7", item_uuid=""), ItemStatus.unchecked())
    rows = merge([orphan], [from_draft(a_draft(id="7"))])
    assert len(rows) == 1


def test_a_live_deposit_and_a_draft_both_appear() -> None:
    live = from_deposit(a_deposit(), ItemStatus(uuid="item-1", found=True, in_archive=True))
    rows = merge([live], [from_draft(a_draft())])
    assert {row.state for row in rows} == {State.LIVE, State.DRAFT}


def test_rows_are_newest_first_across_both_sources() -> None:
    old = from_deposit(
        a_deposit(wsi_id="1", item_uuid="item-1", deposited_at="2026-01-01T00:00:00+00:00"),
        ItemStatus(uuid="item-1", found=True, in_archive=True),
    )
    new = from_deposit(
        a_deposit(wsi_id="2", item_uuid="item-2", deposited_at="2026-07-30T00:00:00+00:00"),
        ItemStatus(uuid="item-2", found=True, in_archive=True),
    )
    draft = from_draft(a_draft(id="7", last_modified_iso="2026-05-01T00:00:00Z"))
    rows = merge([old, new], [draft])
    assert [row.item_uuid or row.wsi_id for row in rows] == ["item-2", "7", "item-1"]


def test_an_undateable_row_sorts_last_rather_than_reordering_the_rest() -> None:
    """One junk timestamp must not shuffle a page of good ones."""
    undated = from_deposit(
        a_deposit(wsi_id="1", item_uuid="item-1", deposited_at="sometime last Tuesday"),
        ItemStatus(uuid="item-1", found=True, in_archive=True),
    )
    dated = from_deposit(
        a_deposit(wsi_id="2", item_uuid="item-2", deposited_at="2026-01-01T00:00:00+00:00"),
        ItemStatus(uuid="item-2", found=True, in_archive=True),
    )
    rows = merge([undated, dated], [])
    assert [row.item_uuid for row in rows] == ["item-2", "item-1"]


def test_the_limit_keeps_the_newest() -> None:
    deposits = [
        from_deposit(
            a_deposit(wsi_id=str(n), item_uuid=f"item-{n:02d}", deposited_at=f"2026-07-{n:02d}"),
            ItemStatus(uuid=f"item-{n:02d}", found=True, in_archive=True),
        )
        for n in range(1, 11)
    ]
    rows = merge(deposits, [], limit=3)
    assert [row.item_uuid for row in rows] == ["item-10", "item-09", "item-08"]


def test_counts_tally_every_state() -> None:
    rows = [
        Submission(state=State.LIVE),
        Submission(state=State.LIVE),
        Submission(state=State.DRAFT),
    ]
    tally = counts(rows)
    assert tally[State.LIVE] == 2
    assert tally[State.DRAFT] == 1
    assert tally[State.WITHDRAWN] == 0


def test_a_draft_row_can_be_rendered_by_the_drafts_partial() -> None:
    """The status page borrows `draft_actions.html`, which takes a Draft."""
    row = from_draft(a_draft())
    back = row.as_draft()
    assert back.id == "7"
    assert back.title == "A Draft"
    assert back.filenames == ["thesis.pdf"]


def test_drafts_never_crowd_a_deposit_off_the_page() -> None:
    """The Phase 10 gate found this: an account with eighty abandoned drafts, all
    touched more recently than any deposit, filled the page — and the deposit
    somebody opened the page to find was the one row missing."""
    deposit = from_deposit(
        a_deposit(wsi_id="1", item_uuid="item-1", deposited_at="2020-01-01T00:00:00+00:00"),
        ItemStatus(uuid="item-1", found=True, in_archive=True, handle="11244/1"),
    )
    # Ids well clear of the deposit's: a workspaceitem id is never reused, so a
    # live draft cannot carry the id the deposit consumed.
    noise = [
        from_draft(a_draft(id=str(100 + n), last_modified_iso=f"2026-07-{n:02d}T00:00:00Z"))
        for n in range(1, 21)
    ]

    rows = merge([deposit], noise, limit=5)

    assert len(rows) == 5
    assert any(row.state is State.LIVE for row in rows), "the deposit keeps its place"
    assert rows[-1].state is State.LIVE, "and is still displayed in date order"


def test_the_order_rows_arrive_in_does_not_decide_which_survive() -> None:
    """Handed oldest-first, the quota must still keep the newest."""
    deposits = [
        from_deposit(
            a_deposit(wsi_id=str(n), item_uuid=f"item-{n:02d}", deposited_at=f"2026-07-{n:02d}"),
            ItemStatus(uuid=f"item-{n:02d}", found=True, in_archive=True),
        )
        for n in range(1, 11)
    ]

    assert [row.item_uuid for row in merge(deposits, [], limit=2)] == ["item-10", "item-09"]
    assert [row.item_uuid for row in merge(list(reversed(deposits)), [], limit=2)] == [
        "item-10",
        "item-09",
    ]
