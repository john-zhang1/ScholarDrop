"""Running the two duplicate checks against a live repository (DESIGN.md §12).

Everything here is best-effort by construction. A missing index, an unreadable
one, a Solr query that fails, a repository that is slow -- all of them mean "no
warning this time", never "the submission cannot continue". The one thing that is
not allowed is a *wrong* warning: a match is confirmed against the repository as
the signed-in user before it is shown.
"""

from __future__ import annotations

import logging
from typing import Any

from scholardrop.config import Instance
from scholardrop.dedup.index import IndexUnavailable, index_path_for, open_index
from scholardrop.dedup.matches import DuplicateMatch, matches_from_index, rank_title_matches
from scholardrop.dspace import DSpaceError
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.dedup")


def index_rows(settings: Settings, instance: Instance, md5: str) -> list:
    """Indexed files with this digest, or nothing if there is no usable index.

    The path is **this instance's** (`index_path_for`), not one shared file: an
    index describes exactly one repository, so a single global path meant only the
    repository it happened to be harvested from ever got an exact-file check, and
    every other instance silently got nothing.

    Opened per call rather than held: the harvester rewrites this file underneath
    a running app, and a lookup is cheap enough that a fresh read-only connection
    costs less than the bookkeeping to notice.
    """
    if not (settings.dedup_enabled and settings.dedup_checksum_enabled and md5):
        return []
    path = index_path_for(
        settings.dedup_checksum_index_path,
        instance.key,
        override=instance.dedup_checksum_index_path,
    )
    try:
        with open_index(path, base_url=instance.base_url) as index:
            return index.lookup(md5, algorithm="MD5")
    except IndexUnavailable as exc:
        log.info("no exact-file check for %s: %s", instance.key, exc)
        return []


async def exact_file_matches(
    client: Any, settings: Settings, instance: Instance, *, md5: str
) -> list[DuplicateMatch]:
    """Items already holding these exact bytes, as far as *this user* can see.

    The index is harvested anonymously, so every row in it is public and no hit
    can disclose a restricted item. The read-back is for staleness rather than
    permission: an indexed item may have been withdrawn, deleted or retitled since
    the harvest, and a warning that links to a 404 is worse than none.

    **Bounded on purpose.** One file can legitimately sit in many items -- a cover
    sheet, a dataset codebook, a boilerplate appendix -- and confirming two hundred
    of them would put two hundred requests inside an upload, which is exactly the
    "no added latency" this design promised. Only as many as the page will show are
    confirmed, with a little slack so a run of stale rows does not leave the panel
    empty when a live one is right behind them.
    """
    rows = index_rows(settings, instance, md5)
    if not rows:
        return []

    wanted = max(settings.dedup_max_matches, 1)
    candidates = matches_from_index(rows, url_for=instance.item_url)
    confirmed = []
    for match in candidates[: wanted * 3]:
        if len(confirmed) >= wanted:
            break
        try:
            live = await client.item(match.item_uuid)
        except DSpaceError as exc:
            log.info("indexed item %s is no longer readable: %s", match.item_uuid, exc)
            continue
        confirmed.append(
            DuplicateMatch(
                title=live.name or match.title,
                kind="file",
                item_uuid=match.item_uuid,
                handle=live.handle or match.handle,
                url=instance.item_url(uuid=match.item_uuid, handle=live.handle or match.handle),
                filename=match.filename,
            )
        )
    if confirmed:
        log.info(
            "exact-file duplicate: %d of %d indexed item(s) hold this file",
            len(confirmed),
            len(candidates),
        )
    return confirmed


async def title_matches(
    client: Any, settings: Settings, instance: Instance, *, title: str, exclude_uuid: str = ""
) -> list[DuplicateMatch]:
    """One phrase query on the title, filtered down to plausible same-work hits.

    Only used where the repository has *not* configured its own duplicate
    detection -- see `section_is_configured`. Quoted so Discovery treats it as a
    phrase; the ranking still returns loosely related items, which
    `rank_title_matches` drops.
    """
    if not (settings.dedup_enabled and settings.dedup_title_query_enabled):
        return []
    # A title is user data going into a Solr phrase query. Inside quotes almost
    # everything is literal, but a `"` closes the phrase and a trailing `\`
    # escapes the closing quote -- both turn a search into a syntax error at best.
    cleaned = " ".join(title.replace('"', " ").replace("\\", " ").split())
    if len(cleaned) < 8:
        # Too short to be evidence of anything: "Report" would match hundreds.
        return []
    try:
        results = await client.search_items(
            f'"{cleaned}"', size=max(settings.dedup_max_matches * 4, 10)
        )
    except DSpaceError as exc:
        # A search that fails is a warning we cannot give, not a page that fails.
        log.info("title duplicate check failed: %s", exc)
        return []
    return rank_title_matches(
        cleaned,
        list(results.items),
        exclude_uuid=exclude_uuid,
        threshold=settings.dedup_title_threshold,
        limit=settings.dedup_max_matches,
        url_for=instance.item_url,
    )
