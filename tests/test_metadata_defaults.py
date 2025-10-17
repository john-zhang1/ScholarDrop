"""Per-collection defaults and "submit another like this" (DESIGN.md §13.4).

The rule every test here is really about: a value nobody typed must never
overwrite one somebody did, and must never reach a field this collection's form
does not configure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholardrop.config import ConfigError, ProfileDefaults, load_mapping_profile
from scholardrop.dspace import DSpaceUnavailable
from scholardrop.submission.defaults import apply_defaults, apply_seed, carry_over
from scholardrop.submission.definition import ConfigCache
from tests.fakes import make_workspaceitem, metadata_values

PROFILE_WITH_DEFAULTS = """
title: dc.title
authors: dc.contributor.author
abstract: dc.description.abstract
doi: dc.identifier.doi

defaults:
  dc.type: Thesis
  dc.language.iso: en
  collections:
    c2:
      dc.type: [Article, Preprint]
"""


def write_profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "shareok-etd.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---- the profile block -----------------------------------------------------


def test_defaults_block_is_read_alongside_the_mapping(tmp_path: Path) -> None:
    profile = load_mapping_profile(write_profile(tmp_path, PROFILE_WITH_DEFAULTS))

    assert profile.metadata_field("title") == "dc.title"
    assert profile.defaults.for_collection("c1") == {
        "dc.type": ["Thesis"],
        "dc.language.iso": ["en"],
    }


def test_a_collections_own_defaults_replace_the_profile_wide_ones(tmp_path: Path) -> None:
    profile = load_mapping_profile(write_profile(tmp_path, PROFILE_WITH_DEFAULTS))

    assert profile.defaults.for_collection("c2") == {
        # Its own answer for the field it names...
        "dc.type": ["Article", "Preprint"],
        # ...and it still inherits the rest.
        "dc.language.iso": ["en"],
    }


def test_a_profile_with_no_defaults_has_none(shipped_config_dir: Path) -> None:
    profile = load_mapping_profile(shipped_config_dir / "mappings" / "shareok-etd.yaml")

    assert profile.defaults.for_collection("c1") == {}


def test_defaults_is_not_read_as_an_extracted_field(tmp_path: Path) -> None:
    """Without the split, `defaults` would fail as an unknown extracted field."""
    body = "title: dc.title\ndefaults:\n  dc.type: x"
    profile = load_mapping_profile(write_profile(tmp_path, body))

    assert "defaults" not in profile.fields


def test_a_default_must_name_a_metadata_field(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a schema.element"):
        load_mapping_profile(write_profile(tmp_path, "title: dc.title\ndefaults:\n  type: Thesis"))


def test_a_default_must_be_a_value_a_field_can_hold(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a value"):
        load_mapping_profile(
            write_profile(tmp_path, "title: dc.title\ndefaults:\n  dc.type: {a: b}")
        )


def test_collections_must_be_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="collection -> values"):
        load_mapping_profile(
            write_profile(tmp_path, "title: dc.title\ndefaults:\n  collections: [c1]")
        )


# ---- writing them into a draft ---------------------------------------------


@pytest.fixture
def profile(shipped_config_dir: Path):
    """The shipped profile, with defaults a test can set."""
    return load_mapping_profile(shipped_config_dir / "mappings" / "shareok-etd.yaml")


async def defaults_on(fake_dspace, item, profile, block: dict) -> dict:
    profile.defaults = ProfileDefaults.parse(block)
    return await apply_defaults(
        fake_dspace, item, profile=profile, cache=ConfigCache(), instance_key="shareok"
    )


async def test_defaults_are_patched_into_a_new_draft(fake_dspace, profile) -> None:
    item = make_workspaceitem("42", collection_uuid="c1")

    applied = await defaults_on(fake_dspace, item, profile, {"dc.type": "Thesis"})

    assert applied == {"dc.type": ["Thesis"]}
    assert fake_dspace.patches == [
        [
            {
                "op": "add",
                "path": "/sections/traditionalpageone/dc.type",
                "value": [{"value": "Thesis"}],
            }
        ]
    ]


async def test_a_field_the_draft_already_holds_is_left_alone(fake_dspace, profile) -> None:
    """A draft made from a registry record arrives with metadata, and it wins."""
    item = make_workspaceitem(
        "42",
        collection_uuid="c1",
        sections={"traditionalpageone": {"dc.type": metadata_values("Article")}},
    )

    applied = await defaults_on(fake_dspace, item, profile, {"dc.type": "Thesis"})

    assert applied == {}
    assert fake_dspace.patches == []


async def test_a_default_this_form_has_no_field_for_is_dropped(fake_dspace, profile) -> None:
    item = make_workspaceitem("42", collection_uuid="c1")

    applied = await defaults_on(fake_dspace, item, profile, {"thesis.degree.level": "Doctoral"})

    assert applied == {}
    assert fake_dspace.patches == []


async def test_a_single_valued_field_takes_the_first_default_only(fake_dspace, profile) -> None:
    item = make_workspaceitem("42", collection_uuid="c1")

    # dc.title is mandatory and not repeatable on the shipped traditional form.
    applied = await defaults_on(fake_dspace, item, profile, {"dc.title": ["One", "Two"]})

    assert applied == {"dc.title": ["One"]}


async def test_a_repository_that_refuses_the_patch_costs_nothing(fake_dspace, profile) -> None:
    fake_dspace.raise_on["patch_sections"] = DSpaceUnavailable("down", path="/x")
    item = make_workspaceitem("42", collection_uuid="c1")

    applied = await defaults_on(fake_dspace, item, profile, {"dc.type": "Thesis"})

    assert applied == {}


async def test_no_defaults_asks_the_repository_nothing(fake_dspace, profile) -> None:
    item = make_workspaceitem("42", collection_uuid="c1")

    assert await defaults_on(fake_dspace, item, profile, {}) == {}
    assert fake_dspace.calls == []


# ---- what the next draft carries -------------------------------------------


def test_carry_over_keeps_the_shared_context(profile) -> None:
    submitted = {
        ("traditionalpageone", "dc.title"): ["A Thesis"],
        ("traditionalpageone", "dc.contributor.author"): ["Chew, Kari"],
        ("traditionalpageone", "dc.type"): ["Thesis"],
        ("traditionalpageone", "dc.date.issued"): ["2016"],
        ("traditionalpagetwo", "dc.description.abstract"): ["Everything about it."],
        ("traditionalpageone", "dc.identifier.doi"): ["10.1000/x"],
        ("traditionalpagetwo", "dc.subject"): ["Chickasaw"],
    }

    assert carry_over(submitted, profile) == {
        "traditionalpageone::dc.type": ["Thesis"],
        "traditionalpageone::dc.date.issued": ["2016"],
        "traditionalpagetwo::dc.subject": ["Chickasaw"],
    }


def test_carry_over_drops_emptied_fields(profile) -> None:
    submitted = {("traditionalpageone", "dc.type"): []}

    assert carry_over(submitted, profile) == {}


async def test_a_seed_naming_an_unconfigured_field_is_dropped(fake_dspace) -> None:
    item = make_workspaceitem("43", collection_uuid="c1")

    applied = await apply_seed(
        fake_dspace,
        item,
        {"traditionalpageone::dc.type": ["Thesis"], "nosuchsection::dc.type": ["Thesis"]},
        cache=ConfigCache(),
        instance_key="shareok",
    )

    assert applied == {"dc.type": ["Thesis"]}


# ---- through the routes ----------------------------------------------------

HTMX = {"HX-Request": "true"}
COMPLETE = [
    ("traditionalpageone::dc.title", "Family at the Heart"),
    ("traditionalpageone::dc.contributor.author", "Chew, Kari"),
    ("traditionalpageone::dc.date.issued", "2016"),
    ("traditionalpageone::dc.type", "Thesis"),
    ("license_granted", "true"),
]


@pytest.fixture
def with_defaults(app):
    """Give the running app's profile a defaults block, as a repository would."""
    profile = app.state.registry.profiles["shareok-etd"]
    profile.defaults = ProfileDefaults.parse({"dc.type": "Thesis"})
    return app


def post(client, path: str, pairs: list[tuple[str, str]]):
    data: dict[str, list[str]] = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return client.post(path, data=data)


def test_uploading_starts_the_draft_with_the_collections_defaults(
    with_defaults, with_collection, fake_dspace, pdf_bytes
) -> None:
    response = with_collection.post(
        "/submit", files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )

    assert response.status_code == 200
    assert fake_dspace.patches == [
        [
            {
                "op": "add",
                "path": "/sections/traditionalpageone/dc.type",
                "value": [{"value": "Thesis"}],
            }
        ]
    ]
    order = [c[0] for c in fake_dspace.calls if c[0] in {"create_workspaceitem", "patch_sections"}]
    assert order == ["create_workspaceitem", "patch_sections"]


def test_defaults_switched_off_write_nothing(
    app, with_defaults, with_collection, fake_dspace, pdf_bytes
) -> None:
    app.state.settings.defaults_enabled = False

    with_collection.post(
        "/submit", files={"file": ("thesis.pdf", pdf_bytes, "application/pdf")}, headers=HTMX
    )

    assert fake_dspace.patches == []


def test_the_confirmation_offers_another_like_this(with_defaults, with_collection) -> None:
    body = post(with_collection, "/review/42/deposit", COMPLETE).text

    assert 'action="/submit/like"' in body
    assert "Submit another like this" in body
    # It says what it will carry -- and what it will not.
    assert "dc.type" in body
    assert "not the title, the authors, or the file" in body


def test_submitting_another_like_it_creates_a_seeded_draft(
    with_defaults, with_collection, fake_dspace
) -> None:
    post(with_collection, "/review/42/deposit", COMPLETE)
    fake_dspace.calls.clear()
    fake_dspace.patches.clear()
    fake_dspace.next_workspaceitem_id = "43"

    response = with_collection.post("/submit/like")

    assert response.status_code == 303
    assert response.headers["location"] == "/review/43"
    args, _ = fake_dspace.call_args("create_workspaceitem")
    assert args[0] == "c1"
    assert len(fake_dspace.patches) == 1
    assert sorted(fake_dspace.patches[0], key=lambda op: op["path"]) == [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.date.issued",
            "value": [{"value": "2016"}],
        },
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.type",
            "value": [{"value": "Thesis"}],
        },
    ]


def test_the_seeded_draft_carries_no_title_author_or_file(
    with_defaults, with_collection, fake_dspace
) -> None:
    post(with_collection, "/review/42/deposit", COMPLETE)
    fake_dspace.patches.clear()
    fake_dspace.next_workspaceitem_id = "43"

    with_collection.post("/submit/like")

    written = {op["path"] for patch in fake_dspace.patches for op in patch}
    assert "/sections/traditionalpageone/dc.title" not in written
    assert "/sections/traditionalpageone/dc.contributor.author" not in written
    assert not fake_dspace.called("upload_bitstream")


def test_another_like_this_without_a_deposit_behind_it_goes_to_browse(with_collection) -> None:
    response = with_collection.post("/submit/like")

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


def test_the_offer_is_absent_when_the_slice_is_off(app, with_collection) -> None:
    app.state.settings.defaults_enabled = False

    body = post(with_collection, "/review/42/deposit", COMPLETE).text

    assert "/submit/like" not in body
