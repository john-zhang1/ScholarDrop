"""Title comparison and match assembly (DESIGN.md §12, layer 2).

The tests that matter here are the *negative* ones. Discovery answers a phrase
query with everything loosely related, and a panel that shows those is a panel
nobody reads by the third submission -- including the time it is right.
"""

from __future__ import annotations

from scholardrop.dedup.matches import (
    matches_from_index,
    matches_from_section,
    rank_title_matches,
    section_is_configured,
)
from scholardrop.dedup.titles import normalise_title, same_work, similarity
from scholardrop.dspace.models import Item
from tests.test_dedup_index import row

TITLE = "Family at the Heart of Chickasaw Language Reclamation"


def item(name: str, uuid: str = "i1", handle: str = "11244/1", issued: str = "") -> Item:
    payload = {
        "uuid": uuid,
        "name": name,
        "handle": handle,
        "metadata": {"dc.date.issued": [{"value": issued}]} if issued else {},
    }
    return Item.from_payload(payload)


# ---- normalisation ----------------------------------------------------------


def test_case_and_whitespace_are_not_differences() -> None:
    """Mirrors what duplicate-detection.cfg does server-side, so the fallback and
    the native detector do not disagree about the easy cases."""
    assert normalise_title("  The  QUICK   Brown Fox ") == "the quick brown fox"


def test_punctuation_is_not_a_difference() -> None:
    assert normalise_title("Reclamation: A Study") == normalise_title("Reclamation — A Study")


def test_accents_are_not_a_difference() -> None:
    """The same title typed on two keyboards is the same title, and a repository
    of any age has both."""
    assert normalise_title("Résumé") == normalise_title("Resume")


def test_identical_after_normalisation_scores_one() -> None:
    assert similarity(TITLE, TITLE.upper() + " ") == 1.0


def test_a_different_work_is_not_the_same_work() -> None:
    assert not same_work(TITLE, "Chickasaws are asking and writing: a language study")


def test_an_empty_title_matches_nothing() -> None:
    assert similarity("", TITLE) == 0.0
    assert not same_work("", "")


# ---- ranking Discovery's answers -------------------------------------------


def test_only_close_titles_survive() -> None:
    """The real shape of a live phrase query: two genuine matches and a hit that
    merely shares words (measured against the local instance, 2026-07-30)."""
    results = [
        item(TITLE, uuid="a", handle="11244/1"),
        item("ScholarDrop Phase 5 gate — " + TITLE, uuid="b"),
        item("Chikashshaat asilhlhat holissochi: Chickasaws are asking", uuid="c"),
    ]
    matches = rank_title_matches(TITLE, results)
    assert [m.item_uuid for m in matches] == ["a"]
    assert matches[0].exact is True


def test_a_near_miss_is_shown_but_not_called_exact() -> None:
    matches = rank_title_matches(TITLE, [item(TITLE + ", 1907-1950", uuid="a")])
    assert len(matches) == 1
    assert matches[0].exact is False


def test_the_draft_never_matches_itself() -> None:
    matches = rank_title_matches(TITLE, [item(TITLE, uuid="mine")], exclude_uuid="mine")
    assert matches == []


def test_exact_matches_rank_above_near_ones() -> None:
    results = [item(TITLE + " revisited", uuid="near"), item(TITLE, uuid="exact")]
    assert [m.item_uuid for m in rank_title_matches(TITLE, results)] == ["exact", "near"]


def test_the_list_is_capped() -> None:
    results = [item(TITLE, uuid=f"i{n}") for n in range(10)]
    assert len(rank_title_matches(TITLE, results, limit=3)) == 3


def test_the_issue_date_comes_along_when_there_is_one() -> None:
    matches = rank_title_matches(TITLE, [item(TITLE, uuid="a", issued="2019")])
    assert matches[0].issued == "2019"


def test_a_url_is_built_when_one_is_offered() -> None:
    matches = rank_title_matches(
        TITLE,
        [item(TITLE, uuid="a", handle="11244/7")],
        url_for=lambda uuid, handle: f"https://ui.test/items/{uuid}",
    )
    assert matches[0].url == "https://ui.test/items/a"


# ---- DSpace's own duplicates section ---------------------------------------


SECTION = {
    "duplicates": {
        "potentialDuplicates": [
            {
                "uuid": "dup-1",
                "title": "An Existing Thesis",
                "owningCollectionName": "Theses",
                "metadata": {"dc.date.issued": [{"value": "2021-05"}]},
            }
        ]
    }
}


def test_an_absent_section_means_the_repository_never_asked() -> None:
    assert section_is_configured({"traditionalpageone": {}}) is False


def test_an_empty_array_means_it_asked_and_found_nothing() -> None:
    """A better answer must not be overruled by ScholarDrop's cruder one."""
    assert section_is_configured({"duplicates": {"potentialDuplicates": []}}) is True
    assert matches_from_section({"duplicates": {"potentialDuplicates": []}}) == []


def test_the_section_is_read_in_the_shape_dspace_sends() -> None:
    matches = matches_from_section(SECTION)
    assert len(matches) == 1
    assert matches[0].title == "An Existing Thesis"
    assert matches[0].collection_name == "Theses"
    assert matches[0].issued == "2021-05"
    assert matches[0].in_progress is False


def test_a_duplicate_that_is_still_a_draft_says_so() -> None:
    """DSpace's detector searches unfinished submissions too, and 'somebody is
    part-way through depositing this' is a different situation from 'it is
    already published'."""
    sections = {
        "duplicates": {"potentialDuplicates": [{"uuid": "d", "title": "T", "workspaceItemId": 9}]}
    }
    assert matches_from_section(sections)[0].in_progress is True


# ---- index rows -------------------------------------------------------------


def test_one_item_holding_a_file_twice_is_one_match() -> None:
    rows = [row(bitstream="bs-1", item="item-1"), row(bitstream="bs-2", item="item-1")]
    assert len(matches_from_index(rows)) == 1


def test_two_items_are_two_matches() -> None:
    rows = [row(bitstream="bs-1", item="item-1"), row(bitstream="bs-2", item="item-2")]
    assert {m.item_uuid for m in matches_from_index(rows)} == {"item-1", "item-2"}
    assert all(m.kind == "file" for m in matches_from_index(rows))
