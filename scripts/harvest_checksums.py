#!/usr/bin/env python
"""Build the local checksum index that layer 1 of duplicate detection reads.

DSpace records an MD5 for every bitstream and offers no way to search by it, so
the question "does this repository already hold these exact bytes?" has to be
answered from an index built in advance (DESIGN.md §12).

    .venv/bin/python scripts/harvest_checksums.py --instance local --full
    .venv/bin/python scripts/harvest_checksums.py --instance local          # incremental

**It signs in as nobody.** One index is shared by every user of an instance, so a
row harvested with privileges would let the app tell submitter A the title and
handle of an item they have no right to know exists. Anonymous crawling keeps the
index to what any reader could have found anyway.

**One request per page, not per item.** `embed=bundles/bitstreams` on the
Discovery query returns each hit's files inline -- exactly at 9.2's
`rest.projections.full.max = 2` -- which is the difference between ~1 000
requests for a 50 000-item repository and ~150 000. Items whose embed is missing
or truncated fall back to walking their bundles, and the summary counts how often
that happened.

**Incremental runs sort by accession date, not modification date.** `lastModified`
is on every item but is not a *configured* Discovery sort field, so it answers 422
(verified on 9.2); the default configuration offers `dc.date.accessioned`. The
honest consequence: a newly archived item is always picked up, and an old item
that gains a file is not. That is what `--full` is for, and why it is worth
running occasionally even after the index exists.

The watermark only moves when a run **completes**. An interrupted harvest that had
already moved it would leave a permanent hole no later run would go back for.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scholardrop.config import load_registry  # noqa: E402
from scholardrop.dedup.index import (  # noqa: E402
    ChecksumIndex,
    IndexedBitstream,
    IndexUnavailable,
    index_path_for,
)
from scholardrop.dspace import DSpaceClient, DSpaceError, DSpaceUnavailable, Item  # noqa: E402
from scholardrop.settings import load_settings  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

#: The bundle holding what a submitter actually deposited. TEXT and THUMBNAIL are
#: derived by DSpace itself -- indexing them would warn about a file the user has
#: never seen, produced from the one they are uploading.
ORIGINAL = "ORIGINAL"

#: `dc.date.accessioned,DESC` is the newest-first sort the default Discovery
#: configuration actually offers; see the module docstring.
DEFAULT_SORT = "dc.date.accessioned,DESC"
ACCESSIONED = "dc.date.accessioned"
EMBED = "bundles/bitstreams"


@dataclass
class Progress:
    """What the run did, for the summary line and for the gate note."""

    requests: int = 0
    pages: int = 0
    items: int = 0
    bitstreams: int = 0
    refetched: int = 0
    skipped_no_files: int = 0
    failures: int = 0
    retries: int = 0
    unembedded_pages: int = 0
    newest_seen: str = ""
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class CountingClient:
    """A DSpaceClient that counts requests and paces itself.

    Wrapping rather than subclassing: the delay belongs to *this* script, not to
    the client every request handler shares.
    """

    def __init__(self, client: DSpaceClient, progress: Progress, delay: float) -> None:
        self._client = client
        self._progress = progress
        self._delay = delay

    async def _pace(self) -> None:
        self._progress.requests += 1
        if self._delay:
            await asyncio.sleep(self._delay)

    async def search_items(self, **kwargs: object) -> object:
        await self._pace()
        return await self._client.search_items(**kwargs)  # type: ignore[arg-type]

    async def bitstreams_in_bundle(self, item_uuid: str, bundle_name: str) -> list:
        # One item's bundles plus one request per bundle; counted as the walk it is.
        await self._pace()
        result = await self._client.bitstreams_in_bundle(item_uuid, bundle_name)
        self._progress.requests += 1
        return result


def accessioned_of(item: Item) -> str:
    """The newest accession timestamp this item has that is actually a timestamp.

    The watermark is compared as a string, which is exact for ISO-8601 and
    nonsense for anything else -- and real repository data is not all ISO-8601.
    On SHAREOK, 35 items carry `dc.date.accessioned = ["today", "2019-07-02T19:20:13Z"]`,
    with the junk at `place 0`. Taking the first value put the literal string
    `today` in the watermark, and because `"t" > "2"` every subsequent incremental
    run would have stopped at its very first item and indexed nothing, for ever,
    without an error. A silent permanent no-op is the worst shape a bug can take.

    So: read every value, keep the ones that parse, and take the newest. An item
    with no parseable date returns "" -- it can never stop the walk and never
    becomes the watermark, so the cost of not understanding it is re-indexing it,
    which is the right way round.
    """
    newest = ""
    for raw in item.values(ACCESSIONED):
        text = raw.strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if text > newest:
            newest = text
    return newest


def rows_for(item: Item, bitstreams: list) -> list[IndexedBitstream]:
    """Index rows for one item's ORIGINAL files, dropping any without a digest."""
    return [
        IndexedBitstream(
            bitstream_uuid=b.uuid,
            checksum=b.checksum,
            algorithm=b.checksum_algorithm or "MD5",
            item_uuid=item.uuid,
            handle=item.handle or "",
            title=item.name,
            filename=b.name,
            size_bytes=b.size_bytes,
        )
        for b in bitstreams
        if b.checksum
    ]


async def files_of(client: CountingClient, item: Item, progress: Progress) -> list:
    """This item's ORIGINAL bitstreams, from the embed or the slow way.

    `bundles_complete` is false when an embedded bitstream list was itself a page
    with more behind it -- rare (a bundle with >20 files) but silent, and a
    silently short file list is a duplicate this index would never find.
    """
    if item.bundles_embedded and item.bundles_complete:
        return item.in_bundle(ORIGINAL)
    progress.refetched += 1
    try:
        return await client.bitstreams_in_bundle(item.uuid, ORIGINAL)
    except DSpaceError as exc:
        progress.failures += 1
        print(f"{YELLOW}  could not read files of {item.uuid}: {exc}{RESET}")
        return []


class EmbedBreaker:
    """Stop paying to rediscover that this stretch of the repository is heavy.

    Digitised material is not scattered evenly: it arrives in accession order, so
    the pages that cannot be embedded come in runs. Detecting a poison page costs
    the full retry ladder (four timeouts plus backoff, ~190 s measured), and paying
    that for every page of a run of two hundred scanned books is most of a crawl.

    So after `trip_after` consecutive fallbacks the embed is skipped outright and
    pages are fetched plainly, which is slower per page but *bounded*; every
    `recheck_every` pages it tries the fast path again, because the run does end
    and the rest of the repository is ordinary. Self-healing rather than a flag
    somebody has to know to set.
    """

    def __init__(self, *, trip_after: int = 2, recheck_every: int = 10) -> None:
        self.trip_after = trip_after
        self.recheck_every = recheck_every
        self.consecutive_failures = 0
        self.tripped = False
        self.since_recheck = 0
        self.trips = 0

    @property
    def skip_embed(self) -> bool:
        """True while the fast path is known to be a waste of time."""
        if not self.tripped:
            return False
        if self.since_recheck >= self.recheck_every:
            self.since_recheck = 0
            return False  # let one page try the embed again
        self.since_recheck += 1
        return True

    def record_failure(self, page: int) -> None:
        self.consecutive_failures += 1
        if not self.tripped and self.consecutive_failures >= self.trip_after:
            self.tripped = True
            self.trips += 1
            self.since_recheck = 0
            print(
                f"{DIM}  {self.consecutive_failures} heavy pages in a row from {page}; "
                f"skipping the embed until it looks worthwhile again{RESET}"
            )

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.tripped:
            self.tripped = False
            self.since_recheck = 0
            print(f"{DIM}  embedding works again; back to one request per page{RESET}")


async def fetch_page(
    client: CountingClient,
    *,
    page: int,
    page_size: int,
    sort: str,
    attempts: int,
    progress: Progress,
    breaker: EmbedBreaker | None = None,
) -> object | None:
    """One page of results: retried, then re-asked without the embed.

    Two different failures hide behind one timeout here, and only the first is the
    one you would guess.

    A **transient** failure -- a dropped connection, a 5xx -- is worth asking
    again, with doubling backoff. The first full crawl of this VM died on a single
    one of these and threw away eight minutes; over a thousand pages that is a
    certainty, not an edge case.

    A **poison page** answers no faster the fourth time. Digitised material is the
    cause: a page of 50 scanned books, each with 74 page images in ORIGINAL and 74
    thumbnails, asks the server to serialise several thousand bitstreams with an
    authorization check apiece, and it simply does not finish. So the last resort
    is the same page *without* `embed`, which returns instantly; `files_of` then
    walks those items one at a time. That costs three requests per item for the
    handful of items that need it, and it loses nothing -- a bundle of 74 exceeds
    the embed's own page size of 20 anyway, so the fast path would have had to
    re-read them regardless.

    A 422 (an unconfigured sort field) or a 403 fails identically every time and
    ends the run at once, so a misconfiguration costs one request rather than
    several minutes of backoff.
    """
    if breaker is not None and breaker.skip_embed:
        try:
            results = await client.search_items(page=page, size=page_size, sort=sort)
        except DSpaceError as exc:
            progress.failures += 1
            print(f"{RED}search failed on page {page}: {exc}{RESET}")
            return None
        progress.unembedded_pages += 1
        return results

    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            results = await client.search_items(page=page, size=page_size, sort=sort, embed=EMBED)
        except DSpaceUnavailable as exc:
            if attempt < attempts:
                progress.retries += 1
                print(f"{YELLOW}  page {page} failed ({exc}); retrying in {delay:.0f}s{RESET}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            print(
                f"{YELLOW}  page {page} will not load with its files embedded; "
                f"asking for it plainly{RESET}"
            )
            try:
                results = await client.search_items(page=page, size=page_size, sort=sort)
            except DSpaceError as plain_exc:
                progress.failures += 1
                print(f"{RED}page {page} failed {attempts} times: {plain_exc}{RESET}")
                return None
            progress.unembedded_pages += 1
            if breaker is not None:
                breaker.record_failure(page)
            return results
        except DSpaceError as exc:
            progress.failures += 1
            print(f"{RED}search failed on page {page}: {exc}{RESET}")
            return None
        else:
            if breaker is not None:
                breaker.record_success()
            return results
    return None


async def harvest(
    client: CountingClient,
    index: ChecksumIndex,
    *,
    watermark: str | None,
    page_size: int,
    max_pages: int | None,
    sort: str,
    progress: Progress,
    verbose: bool,
    start_page: int = 0,
    attempts: int = 4,
) -> bool:
    """Walk newest-first until the watermark. True if the walk ran to the end.

    Returns False when it stopped early -- `--max-pages`, `--start-page`, or a
    page that would not load -- which is what keeps the watermark where it was: a
    partial crawl must not claim to have seen everything down to a date it never
    reached.
    """
    page = start_page
    limit = None if max_pages is None else start_page + max_pages
    breaker = EmbedBreaker()
    while limit is None or page < limit:
        results = await fetch_page(
            client,
            page=page,
            page_size=page_size,
            sort=sort,
            attempts=attempts,
            progress=progress,
            breaker=breaker,
        )
        if results is None:
            print(f"{DIM}  resume this crawl with --start-page {page}{RESET}")
            return False

        progress.pages += 1
        if not results.items:
            return True

        for item in results.items:
            accessioned = accessioned_of(item)
            if watermark and accessioned and accessioned <= watermark:
                # Sorted newest-first, so everything after this is older too.
                if verbose:
                    print(f"{DIM}  reached the watermark at {accessioned}{RESET}")
                return True
            if accessioned > progress.newest_seen:
                progress.newest_seen = accessioned

            bitstreams = await files_of(client, item, progress)
            rows = rows_for(item, bitstreams)
            progress.items += 1
            if not rows:
                progress.skipped_no_files += 1
                continue
            progress.bitstreams += index.upsert(rows)
            if verbose:
                print(f"{DIM}  {item.uuid} {len(rows)} file(s) — {item.name[:60]}{RESET}")

        if not results.has_next:
            return True
        page += 1
        if not verbose and progress.pages % 10 == 0:
            print(
                f"{DIM}  page {page}: {progress.items} items, "
                f"{progress.bitstreams} files, {progress.elapsed:.0f}s{RESET}",
                flush=True,
            )
    return False


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(secret_key="harvest")
    base_url = args.base_url
    override = None
    # The app derives the index path from the instance *key*, so the harvester has
    # to derive it the same way or the two write and read different files -- and
    # the failure would be silence, not an error. Without --instance there is no
    # key, so the host stands in for one; that is a reasonable name and it is why
    # --index exists for anything else.
    key = args.instance or httpx.URL(base_url or "").host or "instance"
    if args.instance:
        registry = load_registry(settings.config_dir)
        instance = registry.instance(args.instance)
        if instance is None:
            print(f"{RED}no instance {args.instance!r} in config/instances.yaml{RESET}")
            return 2
        base_url = instance.base_url
        override = instance.dedup_checksum_index_path
    if not base_url:
        print(f"{RED}give --instance or --base-url{RESET}")
        return 2

    index_path = Path(
        args.index
        or index_path_for(settings.dedup_checksum_index_path, key, override=override)
        or settings.dedup_checksum_index_path
    )
    try:
        index = ChecksumIndex.create(index_path, base_url=base_url)
    except IndexUnavailable as exc:
        print(f"{RED}{exc}{RESET}")
        return 2

    watermark = None if args.full else index.watermark
    progress = Progress()
    print(f"{BOLD}{base_url}{RESET} → {index_path}")
    print(
        f"  {'full rebuild' if args.full else f'incremental from {watermark or "the beginning"}'}"
        f", sort {args.sort}, {args.delay}s between requests"
    )

    async with DSpaceClient(f"{base_url}/api", timeout=args.timeout) as raw:
        completed = await harvest(
            CountingClient(raw, progress, args.delay),
            index,
            watermark=watermark,
            page_size=args.page_size,
            max_pages=args.max_pages,
            sort=args.sort,
            progress=progress,
            verbose=args.verbose,
            start_page=args.start_page,
            attempts=args.attempts,
        )

    print(
        f"\n{progress.items} item(s), {progress.bitstreams} file(s) indexed in "
        f"{progress.elapsed:.0f}s over {progress.requests} request(s)"
    )
    if progress.skipped_no_files:
        print(f"{DIM}  {progress.skipped_no_files} item(s) had no ORIGINAL file{RESET}")
    if progress.refetched:
        print(f"{DIM}  {progress.refetched} item(s) needed a second read for their files{RESET}")
    if progress.retries:
        print(f"{DIM}  {progress.retries} request(s) succeeded on a retry{RESET}")
    if progress.unembedded_pages:
        print(
            f"{DIM}  {progress.unembedded_pages} page(s) were too heavy to embed and "
            f"were walked item by item{RESET}"
        )
    if progress.failures:
        print(f"{YELLOW}  {progress.failures} failure(s){RESET}")

    if completed and progress.newest_seen:
        index.set_watermark(progress.newest_seen)
        print(f"{GREEN}watermark now {progress.newest_seen}{RESET}")
    elif completed:
        print(f"{DIM}nothing new; watermark unchanged{RESET}")
    else:
        print(f"{YELLOW}run did not finish; watermark left at {index.watermark or 'unset'}{RESET}")

    print(f"index holds {index.count()} file(s) across {index.items_indexed()} item(s)")
    index.close()
    return 0 if completed and not progress.failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--instance", help="key from config/instances.yaml")
    parser.add_argument("--base-url", help="DSpace server root, if not using --instance")
    parser.add_argument(
        "--index", help="index file (default: SCHOLARDROP_DEDUP_CHECKSUM_INDEX_PATH)"
    )
    parser.add_argument(
        "--full", action="store_true", help="ignore the watermark and walk everything"
    )
    parser.add_argument("--page-size", type=int, default=50, help="items per request (default: 50)")
    parser.add_argument(
        "--max-pages", type=int, help="stop after this many pages (a test run; keeps the watermark)"
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=0,
        help="resume an interrupted full crawl from this page (never moves the watermark)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=4,
        help="tries per page before giving up, with doubling backoff (default: 4)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="seconds between requests, to stay polite on a live repository (default: 0.2)",
    )
    parser.add_argument(
        "--sort", default=DEFAULT_SORT, help=f"Discovery sort (default: {DEFAULT_SORT})"
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every item")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
