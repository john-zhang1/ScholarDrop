"""ORCID lookup and, more importantly, ORCID *storage* (DESIGN.md §13.2).

The lookup half is assistance and its failure mode is "no suggestions". The
storage half can put a dangling authority key into a live index, so the mode is
pinned here for all three settings, including the one this repository defaults
to: store nothing.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scholardrop.config import Instance, MappingProfile
from scholardrop.orcid import (
    CF_ACCEPTED,
    OrcidPerson,
    build_query,
    is_orcid,
    policy_for,
    search,
)
from scholardrop.settings import load_settings
from scholardrop.submission.patch import build_operations, parse_orcids, parse_submission
from tests.fakes import make_workspaceitem

ORCID = "0000-0002-1825-0097"
API = "https://pub.orcid.org/v3.0"

EXPANDED_SEARCH = {
    "expanded-result": [
        {
            "orcid-id": ORCID,
            "given-names": "Josiah",
            "family-names": "Carberry",
            "credit-name": None,
            "other-name": [],
            "email": [],
            "institution-name": ["Brown University", "Wesleyan University"],
        },
        {
            "orcid-id": "0000-0001-8662-6087",
            "given-names": "Josiah",
            "family-names": "Carberry",
            "credit-name": None,
            "institution-name": [],
        },
    ],
    "num-found": 2,
}

PROFILE = MappingProfile(
    name="shareok-etd",
    fields={
        "title": "dc.title",
        "authors": "dc.contributor.author",
        "advisor": "dc.contributor.advisor",
        "committee_members": "dc.contributor.committeeMember",
    },
)


def instance(orcid_mode: str = "display_only") -> Instance:
    return Instance(
        key="test",
        name="Test",
        base_url="https://dspace.test/server",
        mapping_profile="shareok-etd",
        orcid_mode=orcid_mode,
    )


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        env="dev", secret_key="test-secret", upload_dir=tmp_path / "uploads", orcid_api_url=API
    )


# ---- the query -------------------------------------------------------------


def test_a_form_style_name_searches_the_name_fields() -> None:
    """ "Carberry, Josiah" is far more precise than the same words loose."""
    assert build_query("Carberry, Josiah") == ('family-name:"Carberry" AND given-names:"Josiah"')


def test_a_plain_name_is_searched_as_typed() -> None:
    assert build_query("Josiah Carberry") == '"Josiah Carberry"'


def test_a_pasted_id_looks_itself_up() -> None:
    assert build_query(ORCID) == f"orcid:{ORCID}"


def test_lucene_syntax_in_a_name_is_escaped_not_obeyed() -> None:
    """Unescaped, "Smith-Jones" is the query "Smith NOT Jones"."""
    assert build_query("Smith-Jones") == r'"Smith\-Jones"'
    assert build_query("O'Brien: a name") == '"O\'Brien\\: a name"'


def test_what_counts_as_an_id() -> None:
    assert is_orcid(ORCID)
    assert is_orcid("0000-0002-1825-009X")
    assert not is_orcid("0000-0002-1825")
    assert not is_orcid("Carberry, Josiah")


# ---- the lookup ------------------------------------------------------------


@respx.mock
async def test_a_search_returns_people_with_what_tells_them_apart(settings) -> None:
    route = respx.get(f"{API}/expanded-search/").mock(
        return_value=httpx.Response(200, json=EXPANDED_SEARCH)
    )

    people = await search("Carberry, Josiah", settings)

    assert [p.orcid_id for p in people] == [ORCID, "0000-0001-8662-6087"]
    assert people[0].name == "Carberry, Josiah"
    assert people[0].affiliation == "Brown University"
    assert ORCID in people[0].label
    assert route.calls.last.request.headers["accept"] == "application/json"


@respx.mock
async def test_a_lookup_that_fails_is_silent(settings) -> None:
    """Assistance that is unavailable is not the submitter's problem."""
    respx.get(f"{API}/expanded-search/").mock(side_effect=httpx.ConnectError("down"))

    assert await search("Carberry, Josiah", settings) == []


@respx.mock
async def test_a_lookup_that_is_refused_is_silent(settings) -> None:
    respx.get(f"{API}/expanded-search/").mock(return_value=httpx.Response(503))

    assert await search("Carberry, Josiah", settings) == []


@respx.mock
async def test_two_characters_are_not_a_search(settings) -> None:
    """ "Sm" matches tens of thousands of people and helps nobody."""
    route = respx.get(f"{API}/expanded-search/").mock(
        return_value=httpx.Response(200, json=EXPANDED_SEARCH)
    )

    assert await search("Sm", settings) == []
    assert not route.called


async def test_the_whole_feature_can_be_switched_off(tmp_path) -> None:
    off = load_settings(
        env="dev", secret_key="s", upload_dir=tmp_path / "uploads", orcid_enabled=False
    )

    assert await search("Carberry, Josiah", off) == []


def test_a_person_with_no_name_still_renders() -> None:
    person = OrcidPerson(orcid_id=ORCID)

    assert person.name == ORCID
    assert person.label == ORCID


# ---- which fields offer it -------------------------------------------------


def test_the_lookup_is_offered_on_the_profile_s_people_fields() -> None:
    policy = policy_for(instance(), PROFILE)

    assert policy.offers_lookup("dc.contributor.author")
    assert policy.offers_lookup("dc.contributor.advisor")
    assert policy.offers_lookup("dc.contributor.committeeMember")
    assert not policy.offers_lookup("dc.title")


def test_a_profile_that_maps_no_people_offers_nothing() -> None:
    bare = MappingProfile(name="bare", fields={"title": "dc.title"})

    assert policy_for(instance(), bare).author_fields == frozenset()


# ---- storage: the part with teeth ------------------------------------------


def form_post(*authors: tuple[str, str]) -> list[tuple[str, str]]:
    """A posted form: each author row, then its hidden iD, in document order."""
    items = []
    for name, orcid in authors:
        items.append(("traditionalpageone::dc.contributor.author", name))
        items.append(("traditionalpageone::dc.contributor.author::orcid", orcid))
    return items


ALLOWED: dict = {
    ("traditionalpageone", "dc.contributor.author"): type("C", (), {"repeatable": True})(),
    ("traditionalpageone", "shareok.orcid"): type("C", (), {"repeatable": False})(),
}


def test_ids_are_paired_with_their_own_row() -> None:
    posted = form_post(("Carberry, Josiah", ORCID), ("Roe, Richard", ""))

    assert parse_submission(posted, ALLOWED) == {
        ("traditionalpageone", "dc.contributor.author"): ["Carberry, Josiah", "Roe, Richard"]
    }
    assert parse_orcids(posted, ALLOWED) == {
        ("traditionalpageone", "dc.contributor.author"): [ORCID, ""]
    }


def test_an_emptied_row_does_not_hand_its_id_to_the_next_author() -> None:
    """The reason rows are paired in document order rather than by index."""
    posted = form_post(("", ORCID), ("Roe, Richard", ""))

    assert parse_orcids(posted, ALLOWED) == {}


def test_something_that_is_not_an_id_is_not_stored() -> None:
    posted = form_post(("Carberry, Josiah", "not-an-orcid"))

    assert parse_orcids(posted, ALLOWED) == {}


def test_display_only_writes_no_id_anywhere() -> None:
    """The default, and the safe one."""
    posted = form_post(("Carberry, Josiah", ORCID))
    policy = policy_for(instance("display_only"), PROFILE, ALLOWED)

    operations = build_operations(
        make_workspaceitem(),
        parse_submission(posted, ALLOWED),
        orcids=parse_orcids(posted, ALLOWED),
        policy=policy,
    )

    assert operations == [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.contributor.author",
            "value": [{"value": "Carberry, Josiah"}],
        }
    ]


def test_authority_mode_writes_the_id_as_the_authority_key() -> None:
    posted = form_post(("Carberry, Josiah", ORCID), ("Roe, Richard", ""))
    policy = policy_for(instance("authority"), PROFILE, ALLOWED)

    operations = build_operations(
        make_workspaceitem(),
        parse_submission(posted, ALLOWED),
        orcids=parse_orcids(posted, ALLOWED),
        policy=policy,
    )

    assert operations[0]["value"] == [
        {"value": "Carberry, Josiah", "authority": ORCID, "confidence": CF_ACCEPTED},
        {"value": "Roe, Richard"},
    ]


def test_field_mode_writes_the_id_to_the_parallel_field() -> None:
    posted = form_post(("Carberry, Josiah", ORCID))
    policy = policy_for(instance("field:shareok.orcid"), PROFILE, ALLOWED)

    operations = build_operations(
        make_workspaceitem(),
        parse_submission(posted, ALLOWED),
        orcids=parse_orcids(posted, ALLOWED),
        policy=policy,
    )

    assert {
        "op": "add",
        "path": "/sections/traditionalpageone/shareok.orcid",
        "value": [{"value": ORCID}],
    } in operations
    # ... and never as an authority key as well.
    assert operations[0]["value"] == [{"value": "Carberry, Josiah"}]


def test_a_single_valued_target_takes_the_first_author_s_id() -> None:
    """shareok.orcid is not repeatable: "the ORCID of the primary author"."""
    posted = form_post(("Carberry, Josiah", ORCID), ("Roe, Richard", "0000-0001-8662-6087"))
    policy = policy_for(instance("field:shareok.orcid"), PROFILE, ALLOWED)

    operations = build_operations(
        make_workspaceitem(),
        parse_submission(posted, ALLOWED),
        orcids=parse_orcids(posted, ALLOWED),
        policy=policy,
    )

    target = next(o for o in operations if o["path"].endswith("shareok.orcid"))
    assert target["value"] == [{"value": ORCID}]


def test_a_target_this_collection_does_not_have_is_reported_not_sent() -> None:
    """DSpace only accepts operations on fields the form declares."""
    without = {
        ("traditionalpageone", "dc.contributor.author"): ALLOWED[
            ("traditionalpageone", "dc.contributor.author")
        ]
    }
    posted = form_post(("Carberry, Josiah", ORCID))
    policy = policy_for(instance("field:shareok.orcid"), PROFILE, without)

    assert policy.unavailable_target == "shareok.orcid"
    assert not policy.stores_anything

    operations = build_operations(
        make_workspaceitem(),
        parse_submission(posted, ALLOWED),
        orcids=parse_orcids(posted, ALLOWED),
        policy=policy,
    )
    assert not any("shareok.orcid" in o["path"] for o in operations)


def test_an_unreadable_mode_fails_at_startup() -> None:
    """Config errors belong at boot, not at deposit."""
    with pytest.raises(ValueError, match="orcid_mode"):
        instance("field:")
    with pytest.raises(ValueError, match="orcid_mode"):
        instance("authority-ish")
