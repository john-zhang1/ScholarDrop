"""Type-ahead for a controlled-vocabulary field (DESIGN.md §13.4).

What this replaces is not a dropdown: a vocabulary DSpace *can* list is already a
`<select>`, and stays one. It replaces the **bare text box** a field degrades to
when its vocabulary answers 422 to a listing -- which is every vocabulary backed
by a controlled-vocabulary XML file, srsc included.
"""

from __future__ import annotations

import pytest

from scholardrop.config import MappingProfile
from scholardrop.dspace import DSpaceUnavailable, VocabularyEntry
from scholardrop.orcid import policy_for
from scholardrop.submission.form import build_form
from tests.fakes import DEFINITION_SECTIONS, _field, make_workspaceitem


@pytest.fixture
def unlistable(fake_dspace):
    """The state a real DSpace is in: the vocabulary exists, but cannot be listed."""
    fake_dspace.vocabularies = {}
    fake_dspace.searchable_vocabularies["common_types"] = [
        VocabularyEntry(value="Thesis", display="Thesis"),
        VocabularyEntry(value="Technical Report", display="Technical Report"),
    ]
    return fake_dspace


# ---- what the field renders as ---------------------------------------------


def test_a_listable_vocabulary_is_still_a_select(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert '<option value="Thesis"' in body
    assert "/review/42/vocabulary?vocabulary=common_types" not in body


def test_an_unlistable_vocabulary_becomes_a_search_box(unlistable, with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert "/review/42/vocabulary?vocabulary=common_types" in body
    assert 'class="vocabulary-suggestions"' in body


def test_the_slice_can_be_switched_off(app, unlistable, with_collection) -> None:
    app.state.settings.vocabulary_typeahead_enabled = False

    body = with_collection.get("/review/42").text

    assert "/review/42/vocabulary" not in body


def test_an_appended_row_searches_the_vocabulary_too(unlistable, with_collection) -> None:
    body = with_collection.get("/review/42/row?section=traditionalpageone&field=dc.type").text

    assert "/review/42/vocabulary?vocabulary=common_types" in body


def test_an_author_field_keeps_its_orcid_lookup() -> None:
    """One input carries one hx-get, and a person's name is not a vocabulary term."""
    config = {
        "id": "traditionalpageone",
        "rows": [
            {"fields": [_field("Author", "dc.contributor.author", vocabulary="AuthorAuthority")]}
        ],
    }
    profile = MappingProfile(name="p", fields={"authors": "dc.contributor.author"})

    form = build_form(
        make_workspaceitem(),
        {"traditionalpageone": config},
        sections=DEFINITION_SECTIONS,
        orcid_policy=policy_for(_instance(), profile),
        vocabulary_typeahead=True,
    )

    field = form.field("traditionalpageone", "dc.contributor.author")
    assert field.orcid_lookup is True
    assert field.vocabulary_lookup is False


def _instance():
    from scholardrop.config import Instance

    return Instance(key="shareok", name="SHAREOK", base_url="https://x/server", mapping_profile="p")


# ---- the endpoint -----------------------------------------------------------


def test_suggestions_come_from_the_repositorys_own_vocabulary(
    unlistable, with_collection, fake_dspace
) -> None:
    body = with_collection.get("/review/42/vocabulary?vocabulary=common_types&q=thes&field=x").text

    assert "Thesis" in body
    args, kwargs = fake_dspace.call_args("vocabulary_matches")
    assert args[0] == "common_types"
    assert kwargs["filter"] == "thes"


def test_the_chosen_value_is_what_gets_written(unlistable, with_collection) -> None:
    """A hierarchical entry's display is a whole branch; the value is deposited."""
    unlistable.searchable_vocabularies["srsc"] = [
        VocabularyEntry(value="Linguistics", display="Humanities::Linguistics")
    ]

    body = with_collection.get("/review/42/vocabulary?vocabulary=srsc&q=ling&field=x").text

    assert 'data-value="Linguistics"' in body
    assert "Humanities::Linguistics" in body


def test_a_short_query_asks_the_repository_nothing(
    unlistable, with_collection, fake_dspace
) -> None:
    with_collection.get("/review/42/vocabulary?vocabulary=common_types&q=t")

    assert not fake_dspace.called("vocabulary_matches")


def test_a_vocabulary_name_that_is_not_one_asks_nothing(
    unlistable, with_collection, fake_dspace
) -> None:
    """It is interpolated into a request path, so it is checked, not trusted."""
    response = with_collection.get("/review/42/vocabulary?vocabulary=../../core/items&q=thesis")

    assert response.status_code == 200
    assert not fake_dspace.called("vocabulary_matches")


def test_an_unreachable_vocabulary_leaves_the_box_usable(
    unlistable, with_collection, fake_dspace
) -> None:
    fake_dspace.raise_on["vocabulary_matches"] = DSpaceUnavailable("down", path="/x")

    response = with_collection.get("/review/42/vocabulary?vocabulary=common_types&q=thesis")

    assert response.status_code == 200
    assert "Typing a value is still allowed" in response.text


def test_the_endpoint_answers_nothing_when_the_slice_is_off(
    app, unlistable, with_collection, fake_dspace
) -> None:
    app.state.settings.vocabulary_typeahead_enabled = False

    with_collection.get("/review/42/vocabulary?vocabulary=common_types&q=thesis")

    assert not fake_dspace.called("vocabulary_matches")


def test_the_endpoint_needs_a_session(web) -> None:
    response = web.get("/review/42/vocabulary?vocabulary=common_types&q=thesis")

    assert response.status_code == 303
