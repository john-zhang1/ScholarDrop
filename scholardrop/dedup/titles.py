"""Comparing titles, for the layer-2 fallback (DESIGN.md §12).

Only used when the repository has *not* enabled DSpace's own duplicate detection.
In that case ScholarDrop asks Discovery for the title as a phrase and has to
decide, itself, which of the answers are actually the same work.

That decision matters more than it looks. `discover/search/objects` is a relevance
search: a five-word title will return items sharing three of those words, ranked
below the real matches but returned all the same. Showing those in a panel headed
"possibly already in the repository" teaches the reader that the panel is noise,
and the next time it is right they will not read it either. So the bar is high and
the wording is exact: an identical title after normalisation says "the same
title", and anything else has to be close enough that a person would agree at a
glance.

The normalisation deliberately mirrors what `duplicate-detection.cfg` does
server-side (lowercase, collapse whitespace) so the fallback and the native
detector do not disagree about the easy cases.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Close enough that a person reading the two side by side would call them the same
# work: it tolerates a subtitle's punctuation, a stray "the", "&" for "and". Below
# this the answer is "we do not know", and we say nothing rather than guess.
SAME_WORK_RATIO = 0.90

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Lowercase, unaccent, drop punctuation, collapse whitespace.

    Unicode is folded to NFKD and stripped of combining marks so "Résumé" and
    "Resume" compare equal -- the same title typed on two different keyboards is
    the same title, and a repository has both.
    """
    if not title:
        return ""
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _PUNCTUATION.sub(" ", folded.lower())
    return _WHITESPACE.sub(" ", folded).strip()


def similarity(left: str, right: str) -> float:
    """0..1 on the normalised forms. 1.0 means identical after normalisation."""
    a, b = normalise_title(left), normalise_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def same_work(left: str, right: str, *, threshold: float = SAME_WORK_RATIO) -> bool:
    return similarity(left, right) >= threshold
