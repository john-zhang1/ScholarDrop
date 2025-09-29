"""Duplicate detection (DESIGN.md §12).

Two layers, both **advisory**. Layer 1 hashes the upload and asks a local SQLite
index whether the repository already holds those exact bytes. Layer 2 asks DSpace
whether it already holds this *work*, by title. Neither ever blocks a deposit:
repositories hold legitimate near-duplicates, and the person submitting knows
things this code does not.
"""

from scholardrop.dedup.index import (
    ChecksumIndex,
    IndexedBitstream,
    IndexUnavailable,
    index_path_for,
    open_index,
)
from scholardrop.dedup.matches import (
    DUPLICATES_SECTION,
    DuplicateMatch,
    matches_from_index,
    matches_from_section,
    rank_title_matches,
    section_is_configured,
)
from scholardrop.dedup.titles import normalise_title, same_work, similarity

__all__ = [
    "DUPLICATES_SECTION",
    "ChecksumIndex",
    "DuplicateMatch",
    "IndexUnavailable",
    "IndexedBitstream",
    "index_path_for",
    "matches_from_index",
    "matches_from_section",
    "normalise_title",
    "open_index",
    "rank_title_matches",
    "same_work",
    "section_is_configured",
    "similarity",
]
