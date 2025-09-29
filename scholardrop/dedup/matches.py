"""What a duplicate warning is made of (DESIGN.md §12).

One shape, `DuplicateMatch`, for both layers, because the page renders them the
same way and the difference that matters to the reader is not "which layer found
this" but **how certain it is**: identical bytes, an identical title, or a title
close enough to be worth a look. `kind` and `exact` carry that, and the templates
say it in words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from scholardrop.dedup.index import IndexedBitstream
from scholardrop.dedup.titles import SAME_WORK_RATIO, normalise_title, similarity

MatchKind = Literal["file", "title"]

#: The submission section DSpace's Basic Duplicate Detection contributes, when the
#: repository has configured it. Both the step id and its `sectionType` in
#: `item-submission.xml` are this word.
DUPLICATES_SECTION = "duplicates"


@dataclass(frozen=True)
class DuplicateMatch:
    """One thing already in the repository that this submission might repeat."""

    title: str
    kind: MatchKind
    item_uuid: str = ""
    handle: str = ""
    url: str = ""
    #: Layer 1 only: the name the identical file is stored under.
    filename: str = ""
    collection_name: str = ""
    issued: str = ""
    #: Layer 2 only: was the title identical after normalisation, or merely close?
    exact: bool = False
    #: The "duplicate" is somebody's unfinished submission rather than a
    #: published item -- true of DSpace's own detector, which searches drafts too.
    in_progress: bool = False

    @property
    def display_title(self) -> str:
        return self.title or "Untitled"


def section_is_configured(sections: dict[str, Any]) -> bool:
    """Did the repository run its own duplicate check on this draft?

    An empty `potentialDuplicates` array means "asked, nothing found"; an absent
    section means "never asked". Conflating the two would send ScholarDrop off to
    run its own cruder title query behind a detector that had already answered
    properly -- and a *better* answer would be overruled by a worse one.
    """
    data = sections.get(DUPLICATES_SECTION)
    return isinstance(data, dict) and isinstance(data.get("potentialDuplicates"), list)


def matches_from_section(sections: dict[str, Any], *, url_for: Any = None) -> list[DuplicateMatch]:
    """Turn the `duplicates` section into matches.

    Shape, from `DataDuplicateDetection` / `PotentialDuplicateRest` in 9.2:
    `{"potentialDuplicates": [{uuid, title, owningCollectionName, workspaceItemId,
    workflowItemId, metadata}]}`. `dc.title` is deliberately *not* among the
    copied metadata -- the title is a field of its own -- so do not go looking for
    it there.
    """
    data = sections.get(DUPLICATES_SECTION)
    if not isinstance(data, dict):
        return []
    found: list[DuplicateMatch] = []
    for entry in data.get("potentialDuplicates") or []:
        if not isinstance(entry, dict):
            continue
        uuid = str(entry.get("uuid") or "")
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        found.append(
            DuplicateMatch(
                title=str(entry.get("title") or ""),
                kind="title",
                item_uuid=uuid,
                collection_name=str(entry.get("owningCollectionName") or ""),
                issued=_metadata_value(metadata, "dc.date.issued"),
                url=url_for(uuid=uuid, handle="") if url_for else "",
                exact=True,
                in_progress=bool(entry.get("workspaceItemId") or entry.get("workflowItemId")),
            )
        )
    return found


def rank_title_matches(
    title: str,
    items: list[Any],
    *,
    exclude_uuid: str = "",
    threshold: float = SAME_WORK_RATIO,
    limit: int = 5,
    url_for: Any = None,
) -> list[DuplicateMatch]:
    """Filter Discovery's answers down to the ones that are really the same work.

    Discovery ranks by relevance, so the tail of a phrase query is items that
    merely share words. Everything below `threshold` is dropped rather than shown
    with a low score: a panel that is right nine times in ten is read, and one
    that is right half the time is dismissed unread, including the time it
    mattered.
    """
    normalised = normalise_title(title)
    if not normalised:
        return []
    scored: list[tuple[float, DuplicateMatch]] = []
    for item in items:
        uuid = getattr(item, "uuid", "")
        if exclude_uuid and uuid == exclude_uuid:
            continue
        candidate = getattr(item, "name", "") or ""
        score = similarity(title, candidate)
        if score < threshold:
            continue
        handle = getattr(item, "handle", "") or ""
        scored.append(
            (
                score,
                DuplicateMatch(
                    title=candidate,
                    kind="title",
                    item_uuid=uuid,
                    handle=handle,
                    url=url_for(uuid=uuid, handle=handle) if url_for else "",
                    issued=(item.first("dc.date.issued") if hasattr(item, "first") else ""),
                    exact=normalise_title(candidate) == normalised,
                ),
            )
        )
    scored.sort(key=lambda pair: (-pair[0], pair[1].title))
    return [match for _, match in scored[:limit]]


def matches_from_index(
    rows: list[IndexedBitstream], *, url_for: Any = None
) -> list[DuplicateMatch]:
    """Collapse indexed bitstreams into one match per item.

    An item can hold the same file twice, and two items can hold one file each;
    the reader cares about the items, so the bitstreams are grouped by item and
    the first filename kept as evidence.
    """
    by_item: dict[str, DuplicateMatch] = {}
    for row in rows:
        if row.item_uuid in by_item:
            continue
        by_item[row.item_uuid] = DuplicateMatch(
            title=row.title,
            kind="file",
            item_uuid=row.item_uuid,
            handle=row.handle,
            url=url_for(uuid=row.item_uuid, handle=row.handle) if url_for else "",
            filename=row.filename,
        )
    return list(by_item.values())


def _metadata_value(metadata: dict[str, Any], field: str) -> str:
    entries = metadata.get(field) or []
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if isinstance(entry, dict) and entry.get("value"):
            return str(entry["value"])
    return ""
