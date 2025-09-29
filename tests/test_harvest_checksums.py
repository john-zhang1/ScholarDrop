"""The checksum harvester (DESIGN.md §12).

Payload shapes are copies of what the local DSpace 9.2 answered on 2026-07-30 --
including the two that are easy to get wrong: Discovery's `page` block lives on
the inner `searchResult`, and an *embedded* bitstream list carries a page block of
its own, so a bundle with more files than the embed's page size arrives truncated
without saying so.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from scholardrop.dedup.index import ChecksumIndex
from scholardrop.dspace import DSpaceClient
from scripts.harvest_checksums import ORIGINAL, Progress, harvest

API = "http://dspace.test/server/api"
BASE = "http://dspace.test/server"


def bitstream(uuid: str, name: str, checksum: str, algorithm: str = "MD5") -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "sizeBytes": 1234,
        "checkSum": {"checkSumAlgorithm": algorithm, "value": checksum},
    }


def bundle(name: str, bitstreams: list[dict], *, total: int | None = None) -> dict:
    total = len(bitstreams) if total is None else total
    return {
        "name": name,
        "uuid": f"bundle-{name}",
        "_embedded": {
            "bitstreams": {
                "_embedded": {"bitstreams": bitstreams},
                "page": {
                    "number": 0,
                    "size": 20,
                    "totalElements": total,
                    "totalPages": (total + 19) // 20,
                },
            }
        },
        "_links": {"self": {"href": f"{API}/core/bundles/bundle-{name}"}},
    }


def hit(
    uuid: str,
    *,
    title: str = "A Thesis",
    accessioned: str = "2026-07-01T00:00:00Z",
    bundles: list[dict] | None = None,
) -> dict:
    item: dict = {
        "uuid": uuid,
        "name": title,
        "handle": f"11244/{uuid}",
        "metadata": {"dc.date.accessioned": [{"value": accessioned}]},
        "lastModified": accessioned,
        "_links": {
            "self": {"href": f"{API}/core/items/{uuid}"},
            "bundles": {"href": f"{API}/core/items/{uuid}/bundles"},
        },
    }
    if bundles is not None:
        item["_embedded"] = {"bundles": {"_embedded": {"bundles": bundles}}}
    return {"type": "item", "_embedded": {"indexableObject": item}}


def search_page(hits: list[dict], *, number: int = 0, size: int = 50, total: int | None = None):
    total = len(hits) if total is None else total
    return {
        "_embedded": {
            "searchResult": {
                "_embedded": {"objects": hits},
                "page": {
                    "number": number,
                    "size": size,
                    "totalElements": total,
                    "totalPages": (total + size - 1) // size,
                },
            }
        }
    }


class Client:
    """The two methods `harvest` calls, counted like the real CountingClient."""

    def __init__(self, client: DSpaceClient, progress: Progress) -> None:
        self._client = client
        self._progress = progress

    async def search_items(self, **kwargs):
        self._progress.requests += 1
        return await self._client.search_items(**kwargs)

    async def bitstreams_in_bundle(self, item_uuid: str, bundle_name: str):
        self._progress.requests += 1
        return await self._client.bitstreams_in_bundle(item_uuid, bundle_name)


async def run(
    index: ChecksumIndex,
    *,
    watermark: str | None = None,
    max_pages=None,
    start_page: int = 0,
    attempts: int = 4,
) -> Progress:
    progress = Progress()
    async with DSpaceClient(API) as raw:
        completed = await harvest(
            Client(raw, progress),
            index,
            watermark=watermark,
            page_size=50,
            max_pages=max_pages,
            sort="dc.date.accessioned,DESC",
            progress=progress,
            verbose=False,
            start_page=start_page,
            attempts=attempts,
        )
    progress.completed = completed  # type: ignore[attr-defined]
    return progress


async def _no_wait(_seconds: float) -> None:
    """Backoff without the wait: the delays are the point of the design, not of
    the test, and four real ones would add fourteen seconds to the suite."""


@pytest.fixture
def index(tmp_path: Path) -> ChecksumIndex:
    with ChecksumIndex.create(tmp_path / "checksums.sqlite", base_url=BASE) as store:
        yield store


@respx.mock
async def test_one_request_per_page_indexes_every_original_file(index: ChecksumIndex) -> None:
    """The whole point of the nested embed: a page of items costs one request."""
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [
                    hit("a", bundles=[bundle(ORIGINAL, [bitstream("bs-a", "a.pdf", "aaa")])]),
                    hit("b", bundles=[bundle(ORIGINAL, [bitstream("bs-b", "b.pdf", "bbb")])]),
                ]
            ),
        )
    )
    progress = await run(index)
    assert progress.requests == 1
    assert progress.items == 2
    assert index.count() == 2
    assert index.lookup("aaa")[0].filename == "a.pdf"


@respx.mock
async def test_only_the_original_bundle_is_indexed(index: ChecksumIndex) -> None:
    """TEXT and THUMBNAIL are DSpace's own derivatives. Warning someone that
    their upload matches a thumbnail it produced would be nonsense."""
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [
                    hit(
                        "a",
                        bundles=[
                            bundle(ORIGINAL, [bitstream("bs-a", "a.pdf", "aaa")]),
                            bundle("TEXT", [bitstream("bs-t", "a.pdf.txt", "ttt")]),
                            bundle("THUMBNAIL", [bitstream("bs-h", "a.pdf.jpg", "hhh")]),
                            bundle("LICENSE", [bitstream("bs-l", "license.txt", "lll")]),
                        ],
                    )
                ]
            ),
        )
    )
    await run(index)
    assert index.count() == 1
    assert index.lookup("ttt") == []
    assert index.lookup("lll") == []


@respx.mock
async def test_it_follows_pagination(index: ChecksumIndex) -> None:
    pages = [
        search_page(
            [hit("a", bundles=[bundle(ORIGINAL, [bitstream("bs-a", "a.pdf", "aaa")])])],
            number=0,
            size=1,
            total=2,
        ),
        search_page(
            [hit("b", bundles=[bundle(ORIGINAL, [bitstream("bs-b", "b.pdf", "bbb")])])],
            number=1,
            size=1,
            total=2,
        ),
    ]
    respx.get(f"{API}/discover/search/objects").mock(
        side_effect=[httpx.Response(200, json=p) for p in pages]
    )
    progress = await run(index)
    assert progress.pages == 2
    assert index.count() == 2


@respx.mock
async def test_it_stops_at_the_watermark(index: ChecksumIndex) -> None:
    """Sorted newest-first, so the first item at or below the watermark ends the
    walk -- that is what makes a nightly run cost a handful of requests."""
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [
                    hit(
                        "new",
                        accessioned="2026-07-30T00:00:00Z",
                        bundles=[bundle(ORIGINAL, [bitstream("bs-n", "n.pdf", "nnn")])],
                    ),
                    hit(
                        "old",
                        accessioned="2026-07-01T00:00:00Z",
                        bundles=[bundle(ORIGINAL, [bitstream("bs-o", "o.pdf", "ooo")])],
                    ),
                    hit(
                        "older",
                        accessioned="2026-06-01T00:00:00Z",
                        bundles=[bundle(ORIGINAL, [bitstream("bs-x", "x.pdf", "xxx")])],
                    ),
                ],
                total=3,
            ),
        )
    )
    progress = await run(index, watermark="2026-07-01T00:00:00Z")
    assert progress.items == 1
    assert index.lookup("nnn")
    assert index.lookup("ooo") == []
    assert progress.newest_seen == "2026-07-30T00:00:00Z"


@respx.mock
async def test_a_truncated_embed_is_re_read_rather_than_trusted(index: ChecksumIndex) -> None:
    """An embedded bitstream list is itself a page. A bundle with more files than
    fit is short by default and says so only in its `page` block -- and a
    silently short file list is a duplicate this index would never find."""
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [
                    hit(
                        "a",
                        bundles=[bundle(ORIGINAL, [bitstream("bs-1", "one.pdf", "111")], total=25)],
                    )
                ]
            ),
        )
    )
    respx.get(f"{API}/core/items/a/bundles").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "bundles": [{"uuid": "bundle-ORIGINAL", "name": ORIGINAL, "_links": {}}]
                },
                "page": {"number": 0, "size": 20, "totalElements": 1, "totalPages": 1},
            },
        )
    )
    respx.get(f"{API}/core/bundles/bundle-ORIGINAL/bitstreams").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "bitstreams": [
                        bitstream("bs-1", "one.pdf", "111"),
                        bitstream("bs-2", "two.pdf", "222"),
                    ]
                },
                "page": {"number": 0, "size": 50, "totalElements": 2, "totalPages": 1},
            },
        )
    )
    progress = await run(index)
    assert progress.refetched == 1
    assert index.count() == 2
    assert index.lookup("222")


@respx.mock
async def test_an_item_with_no_embed_at_all_is_walked(index: ChecksumIndex) -> None:
    """If `embed=bundles/bitstreams` is ever unavailable the harvest must still
    work, just slowly -- it is an optimisation, not the mechanism."""
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_page([hit("a")]))
    )
    respx.get(f"{API}/core/items/a/bundles").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "bundles": [{"uuid": "bundle-ORIGINAL", "name": ORIGINAL, "_links": {}}]
                },
                "page": {"number": 0, "size": 20, "totalElements": 1, "totalPages": 1},
            },
        )
    )
    respx.get(f"{API}/core/bundles/bundle-ORIGINAL/bitstreams").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {"bitstreams": [bitstream("bs-1", "one.pdf", "111")]},
                "page": {"number": 0, "size": 50, "totalElements": 1, "totalPages": 1},
            },
        )
    )
    progress = await run(index)
    assert progress.refetched == 1
    assert index.lookup("111")


@respx.mock
async def test_an_item_with_no_files_is_counted_not_indexed(index: ChecksumIndex) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_page([hit("a", bundles=[])]))
    )
    progress = await run(index)
    assert progress.items == 1
    assert progress.skipped_no_files == 1
    assert index.count() == 0


@respx.mock
async def test_a_bitstream_without_a_digest_is_dropped(index: ChecksumIndex) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [hit("a", bundles=[bundle(ORIGINAL, [bitstream("bs-a", "a.pdf", "")])])]
            ),
        )
    )
    await run(index)
    assert index.count() == 0


@respx.mock
async def test_a_partial_run_does_not_claim_to_be_complete(index: ChecksumIndex) -> None:
    """`--max-pages` is a test run. Letting it move the watermark would leave a
    hole in the index that no later run goes back for."""
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [hit("a", bundles=[bundle(ORIGINAL, [bitstream("bs-a", "a.pdf", "aaa")])])],
                size=1,
                total=99,
            ),
        )
    )
    progress = await run(index, max_pages=1)
    assert progress.completed is False


@respx.mock
async def test_a_failed_search_ends_the_run_without_a_watermark(index: ChecksumIndex) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(422, json={"message": "Invalid search request"})
    )
    progress = await run(index)
    assert progress.completed is False
    assert progress.failures == 1


@respx.mock
async def test_a_dropped_connection_is_retried_rather_than_ending_the_crawl(
    index: ChecksumIndex, monkeypatch
) -> None:
    """Measured on the real instance: a full crawl died on page 65 to one dropped
    connection and threw away eight minutes. A thousand-page job meets one."""
    monkeypatch.setattr("scripts.harvest_checksums.asyncio.sleep", _no_wait)
    respx.get(f"{API}/discover/search/objects").mock(
        side_effect=[
            httpx.ConnectError("connection reset"),
            httpx.Response(
                200,
                json=search_page(
                    [hit("a", bundles=[bundle(ORIGINAL, [bitstream("bs-a", "a.pdf", "aaa")])])]
                ),
            ),
        ]
    )
    progress = await run(index)

    assert progress.retries == 1
    assert progress.failures == 0
    assert progress.completed is True
    assert index.lookup("aaa")


@respx.mock
async def test_a_page_that_keeps_failing_stops_the_run_and_says_where(
    index: ChecksumIndex, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.harvest_checksums.asyncio.sleep", _no_wait)
    respx.get(f"{API}/discover/search/objects").mock(side_effect=httpx.ConnectError("down"))

    progress = await run(index, attempts=3)

    assert progress.retries == 2
    assert progress.failures == 1
    assert progress.completed is False


@respx.mock
async def test_a_misconfigured_sort_fails_at_once_rather_than_backing_off(
    index: ChecksumIndex, monkeypatch
) -> None:
    """422 means the sort field is not configured on this instance. It will answer
    identically four times, so paying the backoff for it is pure delay."""
    monkeypatch.setattr("scripts.harvest_checksums.asyncio.sleep", _no_wait)
    route = respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(422, json={"message": "Invalid search request"})
    )

    progress = await run(index)

    assert route.call_count == 1
    assert progress.retries == 0
    assert progress.failures == 1


@respx.mock
async def test_resuming_starts_where_it_stopped(index: ChecksumIndex) -> None:
    route = respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_page([], total=0))
    )

    await run(index, start_page=65)

    assert route.calls[0].request.url.params["page"] == "65"


@respx.mock
async def test_a_page_too_heavy_to_embed_is_asked_for_plainly(
    index: ChecksumIndex, monkeypatch
) -> None:
    """Measured on the real instance: page 65 held 50 digitised books with 74 page
    images each, and the nested embed never finished -- not once, not on the
    fourth try. Asking without the embed returns instantly and `files_of` walks
    the items, which is what the fast path would have had to do anyway for a
    bundle of 74."""
    monkeypatch.setattr("scripts.harvest_checksums.asyncio.sleep", _no_wait)

    def answer(request: httpx.Request) -> httpx.Response:
        if "embed" in request.url.params:
            raise httpx.ReadTimeout("too much to serialise")
        return httpx.Response(200, json=search_page([hit("a")]))

    respx.get(f"{API}/discover/search/objects").mock(side_effect=answer)
    respx.get(f"{API}/core/items/a/bundles").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "bundles": [{"uuid": "bundle-ORIGINAL", "name": ORIGINAL, "_links": {}}]
                },
                "page": {"number": 0, "size": 20, "totalElements": 1, "totalPages": 1},
            },
        )
    )
    respx.get(f"{API}/core/bundles/bundle-ORIGINAL/bitstreams").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {"bitstreams": [bitstream("bs-1", "page-01.jpg", "111")]},
                "page": {"number": 0, "size": 50, "totalElements": 1, "totalPages": 1},
            },
        )
    )

    progress = await run(index)

    assert progress.unembedded_pages == 1
    assert progress.failures == 0
    assert progress.completed is True
    assert index.lookup("111"), "the heavy page must still be indexed, just slowly"


def test_the_breaker_trips_on_a_run_of_heavy_pages_and_recovers() -> None:
    """Digitised material arrives in accession order, so heavy pages come in runs.
    Paying ~190 s of timeouts to rediscover that for each page of a two-hundred-page
    run is most of a crawl -- so the fast path is skipped, then re-probed, because
    the run ends and the rest of the repository is ordinary."""
    from scripts.harvest_checksums import EmbedBreaker

    breaker = EmbedBreaker(trip_after=2, recheck_every=3)
    assert breaker.skip_embed is False

    breaker.record_failure(65)
    assert breaker.skip_embed is False, "one heavy page could still be bad luck"
    breaker.record_failure(66)
    assert breaker.tripped is True

    # Skipped for the next `recheck_every` pages, then one page tries again.
    assert [breaker.skip_embed for _ in range(4)] == [True, True, True, False]

    # That probe worked, so the fast path comes straight back.
    breaker.record_success()
    assert breaker.tripped is False
    assert breaker.skip_embed is False


def test_a_success_between_heavy_pages_resets_the_count() -> None:
    """Two failures either side of a good page are two coincidences, not a run."""
    from scripts.harvest_checksums import EmbedBreaker

    breaker = EmbedBreaker(trip_after=2)
    breaker.record_failure(10)
    breaker.record_success()
    breaker.record_failure(20)

    assert breaker.tripped is False


@respx.mock
async def test_a_tripped_breaker_skips_the_embed_entirely(index: ChecksumIndex) -> None:
    """The point of tripping: no more timeouts at all, not merely fewer."""
    from scripts.harvest_checksums import EmbedBreaker, fetch_page

    route = respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(200, json=search_page([hit("a")]))
    )
    breaker = EmbedBreaker(trip_after=1)
    breaker.record_failure(1)
    progress = Progress()

    async with DSpaceClient(API) as raw:
        await fetch_page(
            Client(raw, progress),
            page=2,
            page_size=50,
            sort="dc.date.accessioned,DESC",
            attempts=4,
            progress=progress,
            breaker=breaker,
        )

    assert route.call_count == 1
    assert "embed" not in route.calls[0].request.url.params
    assert progress.unembedded_pages == 1


def test_junk_in_an_accession_date_cannot_poison_the_watermark() -> None:
    """Real SHAREOK data: 35 items read `["today", "2019-07-02T19:20:13Z"]`, junk
    first. Taking value zero wrote the literal string `today` as the watermark,
    and since "t" > "2" every later incremental run would have stopped at its
    first item and indexed nothing, for ever, with no error anywhere."""
    from scholardrop.dspace.models import Item
    from scripts.harvest_checksums import accessioned_of

    item = Item.from_payload(
        {
            "uuid": "a",
            "metadata": {
                "dc.date.accessioned": [
                    {"value": "today", "place": 0},
                    {"value": "2019-07-02T19:20:13Z", "place": 1},
                ]
            },
        }
    )

    assert accessioned_of(item) == "2019-07-02T19:20:13Z"


def test_an_item_with_no_usable_date_is_never_the_watermark() -> None:
    """It also must not stop the walk. Re-indexing something we cannot date is the
    cheap mistake; stopping the crawl on it is the expensive one."""
    from scholardrop.dspace.models import Item
    from scripts.harvest_checksums import accessioned_of

    item = Item.from_payload(
        {"uuid": "a", "metadata": {"dc.date.accessioned": [{"value": "n.d."}]}}
    )

    assert accessioned_of(item) == ""


@respx.mock
async def test_an_undateable_item_does_not_stop_an_incremental_run(
    index: ChecksumIndex,
) -> None:
    respx.get(f"{API}/discover/search/objects").mock(
        return_value=httpx.Response(
            200,
            json=search_page(
                [
                    {
                        "type": "item",
                        "_embedded": {
                            "indexableObject": {
                                "uuid": "undateable",
                                "name": "No date at all",
                                "metadata": {"dc.date.accessioned": [{"value": "today"}]},
                                "_embedded": {
                                    "bundles": {
                                        "_embedded": {
                                            "bundles": [
                                                bundle(
                                                    ORIGINAL,
                                                    [bitstream("bs-u", "u.pdf", "uuu")],
                                                )
                                            ]
                                        }
                                    }
                                },
                                "_links": {},
                            }
                        },
                    },
                    hit(
                        "newer",
                        accessioned="2026-07-30T00:00:00Z",
                        bundles=[bundle(ORIGINAL, [bitstream("bs-n", "n.pdf", "nnn")])],
                    ),
                ],
                total=2,
            ),
        )
    )

    progress = await run(index, watermark="2020-01-01T00:00:00Z")

    assert index.lookup("uuu"), "an undateable item is indexed, not skipped"
    assert index.lookup("nnn"), "and it did not end the walk"
    assert progress.newest_seen == "2026-07-30T00:00:00Z"
