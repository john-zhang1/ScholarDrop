"""Where is my item? (DESIGN.md §13.3)

The single most common question after a deposit, answered without anyone having
to open a ticket. The page merges what this app logged with what the repository
says now, and is read-only apart from the draft actions it borrows from §6:
approving and rejecting belong to reviewers in DSpace, not here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from scholardrop.config import Instance
from scholardrop.dspace import DSpaceClient, DSpaceError
from scholardrop.dspace.models import ItemStatus
from scholardrop.history import LogUnavailable, State, Submission, counts, merge
from scholardrop.history.log import Deposit
from scholardrop.history.status import from_deposit, from_draft
from scholardrop.routes.deps import (
    dspace_client,
    open_history,
    render,
    require_session,
    session_instance,
)
from scholardrop.session import Session
from scholardrop.submission.drafts import to_draft

router = APIRouter()
log = logging.getLogger("scholardrop.history")

#: Drafts come from DSpace a page at a time. One page is what the drafts list
#: shows; here the merge wants all of them, but "all" has to stop somewhere --
#: somebody with 300 abandoned drafts should get a fast page, not a complete one.
DRAFT_PAGES = 3
DRAFT_PAGE_SIZE = 20


@router.get("/my-submissions", response_class=HTMLResponse)
async def my_submissions(request: Request) -> HTMLResponse:
    require_session(request)
    return render(request, "submissions.html")


@router.get("/my-submissions/list", response_class=HTMLResponse)
async def submissions_list(request: Request) -> HTMLResponse:
    """The merged list. One live lookup per logged deposit, run concurrently.

    Deliberately not paginated. Two sources with independent orderings cannot be
    paged consistently without merging the whole of both anyway, and one person's
    own submissions are few -- so the page shows the most recent
    `submissions_max_rows` and says so when there are more.
    """
    session = require_session(request)
    instance = session_instance(request, session)
    settings = request.app.state.settings

    deposits, logged_total, history_available = _logged_deposits(request, session, instance)
    async with dspace_client(request, session) as client:
        drafts = await _live_drafts(request, client, session, instance)
        statuses = await _resolve(client, deposits, settings.submissions_lookup_concurrency)

    rows = merge(
        (from_deposit(deposit, status) for deposit, status in zip(deposits, statuses, strict=True)),
        [from_draft(draft) for draft in drafts],
        limit=settings.submissions_max_rows,
    )
    tally = counts(rows)
    # The log is read a page at a time, so "is there more?" has to come from the
    # count rather than from what came back -- a read of exactly `max_rows` rows
    # looks identical whether or not it was the lot.
    truncated = logged_total > len(deposits) or len(deposits) + len(drafts) > len(rows)
    return render(
        request,
        "partials/submissions_list.html",
        submissions=rows,
        counts=tally,
        live=tally[State.LIVE],
        in_review=tally[State.IN_REVIEW],
        draft_count=tally[State.DRAFT],
        history_available=history_available,
        truncated=truncated,
    )


def _logged_deposits(
    request: Request, session: Session, instance: Instance
) -> tuple[list[Deposit], int, bool]:
    """This person's deposits here, how many there are in all, and whether the
    log answered at all.

    An unavailable log is not an error page: the drafts half of this page is
    still worth showing, and the template says the deposit history is missing
    rather than implying the person never deposited anything.
    """
    settings = request.app.state.settings
    who = {"base_url": instance.base_url, "eperson_uuid": session.eperson_uuid or ""}
    try:
        with open_history(request) as history:
            return (
                history.for_person(**who, limit=settings.submissions_max_rows),
                history.count_for_person(**who),
                True,
            )
    except LogUnavailable as exc:
        log.info("no deposit history for %s: %s", instance.key, exc)
        return [], 0, False


async def _live_drafts(
    request: Request, client: DSpaceClient, session: Session, instance: Instance
) -> list[Any]:
    """Every draft the repository holds for this user, up to a few pages of them."""
    names: dict[str, str] = {}
    labels_memo: dict[str, Any] = {}
    drafts = []
    for page in range(DRAFT_PAGES):
        results = await client.find_by_submitter(
            session.eperson_uuid, page=page, size=DRAFT_PAGE_SIZE
        )
        for item in results.items:
            drafts.append(
                await to_draft(
                    client,
                    item,
                    names,
                    cache=request.app.state.form_configs,
                    instance_key=instance.key,
                    labels_memo=labels_memo,
                )
            )
        if not results.has_next:
            break
    return drafts


async def _resolve(
    client: DSpaceClient, deposits: list[Deposit], concurrency: int
) -> list[ItemStatus]:
    """Look each logged deposit up live, concurrently and forgivingly.

    **One row that cannot be read must not take the page down.** A submission
    whose lookup fails comes back `unchecked`, which the page renders as
    "could not be checked just now" beside everything that did resolve --
    the per-row degradation §13.3 asks for.
    """
    if not deposits:
        return []
    limit = asyncio.Semaphore(max(concurrency, 1))

    async def one(deposit: Deposit) -> ItemStatus:
        if not deposit.item_uuid:
            # Logged without an item uuid: the deposit succeeded but the item
            # link could not be read at the time, so there is nothing to ask about.
            return ItemStatus.unchecked()
        async with limit:
            try:
                return await client.item_status(deposit.item_uuid)
            except DSpaceError as exc:
                log.info("could not read the state of item %s: %s", deposit.item_uuid, exc)
                return ItemStatus.unchecked(deposit.item_uuid)

    return list(await asyncio.gather(*(one(deposit) for deposit in deposits)))


__all__ = ["Submission", "router"]
