"""Language detection and parallel values (DESIGN.md §13.4).

Two halves of one idea: work out what language the document is in so nobody has
to say so, and let a bilingual item state its title and abstract twice --
carrying the `language` key that makes the second one a translation rather than a
second title.
"""

from __future__ import annotations

import pytest

from scholardrop.extract.language import DETECTED_CONFIDENCE, detect_language
from scholardrop.extract.models import ExtractedMetadata, SourceTier, text
from scholardrop.extract.pipeline import extract_from_document
from scholardrop.extract.reader import PdfDocument
from scholardrop.settings import load_settings
from scholardrop.submission.patch import build_operations, parse_languages, parse_submission
from tests.fakes import make_workspaceitem, metadata_values

ENGLISH = (
    "This dissertation is a study of the sediment that moves through the ephemeral "
    "streams of the southern plains, and of the way that it is deposited in them."
)
SPANISH = (
    "Esta tesis es un estudio de los sedimentos que se mueven por los arroyos "
    "efímeros de las llanuras del sur, y de la forma en que se depositan en ellos."
)
CHINESE = "本论文研究了南部平原季节性河流中泥沙的运移过程，以及泥沙在其中的沉积方式。"
JAPANESE = "本論文では、南部平原の季節河川における土砂の移動とその堆積について研究する。"

TITLE = "traditionalpageone::dc.title"
TITLE_LANG = "traditionalpageone::dc.title::lang"
ALLOWED = {("traditionalpageone", "dc.title"): object()}


# ---- detection --------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "expected"),
    [(ENGLISH, "en"), (SPANISH, "es"), (CHINESE, "zh"), (JAPANESE, "ja")],
)
def test_detects_the_obvious_cases(sample: str, expected: str) -> None:
    code, confidence = detect_language(sample)

    assert code == expected
    assert confidence == DETECTED_CONFIDENCE


def test_says_nothing_about_an_empty_document() -> None:
    assert detect_language("   \n  ") == ("", 0.0)


def test_says_nothing_about_a_scrap() -> None:
    """A title with two function words in it is not evidence of anything."""
    assert detect_language("Prairie Streams and the Sediment in Them")[0] == ""


def test_a_detected_language_is_flagged_for_checking() -> None:
    """Tier-2 confidence, so the review form marks it "check this" like a guess."""
    assert DETECTED_CONFIDENCE < 0.6


async def test_the_pipeline_fills_the_language_it_detected() -> None:
    document = PdfDocument(page_count=10, front_text=ENGLISH)

    metadata = await extract_from_document(
        document, instance=_instance(), settings=load_settings(secret_key="x")
    )

    assert metadata.language.value == "en"
    assert metadata.language.source_tier == SourceTier.HEURISTIC


async def test_a_language_something_else_supplied_is_left_alone(monkeypatch) -> None:
    """A model or a registry record knows more than a word count does."""
    from scholardrop.extract import pipeline

    async def fake_extract(document, **kwargs):
        return ExtractedMetadata(language=text("zh", 0.9, SourceTier.LLM))

    monkeypatch.setattr(pipeline, "_extract", fake_extract)
    document = PdfDocument(page_count=10, front_text=ENGLISH)

    metadata = await extract_from_document(
        document, instance=_instance(), settings=load_settings(secret_key="x")
    )

    assert metadata.language.value == "zh"


async def test_detection_can_be_switched_off() -> None:
    document = PdfDocument(page_count=10, front_text=ENGLISH)

    metadata = await extract_from_document(
        document,
        instance=_instance(),
        settings=load_settings(secret_key="x", bilingual_enabled=False),
    )

    assert metadata.language.present is False


def _instance():
    from scholardrop.config import Instance

    return Instance(
        key="k", name="n", base_url="https://x/server", mapping_profile="p", extraction="heuristic"
    )


# ---- parallel values --------------------------------------------------------


def test_a_language_is_paired_with_the_value_it_follows() -> None:
    posted = [
        (TITLE, "泥沙研究"),
        (TITLE_LANG, "zh"),
        (TITLE, "A Study of Sediment"),
        (TITLE_LANG, "en"),
    ]

    assert parse_languages(posted, ALLOWED) == {("traditionalpageone", "dc.title"): ["zh", "en"]}


def test_an_emptied_row_does_not_hand_its_language_to_the_next() -> None:
    posted = [(TITLE, ""), (TITLE_LANG, "zh"), (TITLE, "A Study of Sediment"), (TITLE_LANG, "en")]

    assert parse_languages(posted, ALLOWED) == {("traditionalpageone", "dc.title"): ["en"]}


def test_a_language_nobody_offered_is_dropped() -> None:
    posted = [(TITLE, "A Study"), (TITLE_LANG, "klingon")]

    assert parse_languages(posted, ALLOWED) == {}


def test_the_patch_carries_the_language_key() -> None:
    submitted = parse_submission(
        [(TITLE, "泥沙研究"), (TITLE_LANG, "zh"), (TITLE, "A Study"), (TITLE_LANG, "en")], ALLOWED
    )
    languages = parse_languages(
        [(TITLE, "泥沙研究"), (TITLE_LANG, "zh"), (TITLE, "A Study"), (TITLE_LANG, "en")], ALLOWED
    )

    operations = build_operations(make_workspaceitem(), submitted, languages=languages)

    assert operations == [
        {
            "op": "add",
            "path": "/sections/traditionalpageone/dc.title",
            "value": [
                {"value": "泥沙研究", "language": "zh"},
                {"value": "A Study", "language": "en"},
            ],
        }
    ]


def test_setting_only_the_language_still_patches() -> None:
    """The stored comparison is of values, so an unchanged text with a new
    language key would otherwise produce no operation at all."""
    item = make_workspaceitem(
        sections={"traditionalpageone": {"dc.title": metadata_values("A Study")}}
    )
    posted = [(TITLE, "A Study"), (TITLE_LANG, "en")]

    operations = build_operations(
        item, parse_submission(posted, ALLOWED), languages=parse_languages(posted, ALLOWED)
    )

    assert operations[0]["value"] == [{"value": "A Study", "language": "en"}]


def test_a_value_with_no_language_is_written_plainly() -> None:
    posted = [(TITLE, "A Study"), (TITLE_LANG, "")]

    operations = build_operations(
        make_workspaceitem(),
        parse_submission(posted, ALLOWED),
        languages=parse_languages(posted, ALLOWED),
    )

    assert operations[0]["value"] == [{"value": "A Study"}]


# ---- through the form -------------------------------------------------------


def test_the_title_offers_a_translation_and_a_language(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert "Add a translation" in body
    assert 'name="traditionalpageone::dc.title::lang"' in body


def test_a_date_does_not(with_collection) -> None:
    body = with_collection.get("/review/42").text

    assert 'name="traditionalpageone::dc.date.issued::lang"' not in body


def test_the_slice_can_be_switched_off(app, with_collection) -> None:
    app.state.settings.bilingual_enabled = False

    body = with_collection.get("/review/42").text

    assert "Add a translation" not in body
    assert "::lang" not in body


def test_a_translation_row_is_served_for_a_single_valued_field(with_collection) -> None:
    """dc.title is not repeatable, and a translation is the one row it accepts."""
    response = with_collection.get(
        "/review/42/row?section=traditionalpageone&field=dc.title&translation=1"
    )

    assert response.status_code == 200
    assert 'name="traditionalpageone::dc.title::lang"' in response.text


def test_a_translation_row_is_refused_for_a_field_that_is_not_words(with_collection) -> None:
    response = with_collection.get(
        "/review/42/row?section=traditionalpageone&field=dc.date.issued&translation=1"
    )

    assert response.text.strip() == ""


def test_a_stored_language_survives_a_resumed_draft(with_collection, fake_dspace) -> None:
    from tests.fakes import make_workspaceitem as workspaceitem

    fake_dspace.workspaceitems["42"] = workspaceitem(
        "42",
        sections={
            "traditionalpageone": {
                "dc.title": [
                    {"value": "泥沙研究", "language": "zh"},
                    {"value": "A Study", "language": "en"},
                ]
            }
        },
    )

    body = with_collection.get("/review/42").text

    assert '<option value="zh" selected>' in body
    assert '<option value="en" selected>' in body


def test_the_language_survives_the_confirm_step(with_collection) -> None:
    body = with_collection.post(
        "/review/42/confirm",
        data={TITLE: "泥沙研究", TITLE_LANG: "zh", "license_granted": "true"},
    ).text

    assert f'name="{TITLE_LANG}" value="zh"' in body


def test_the_deposit_writes_what_the_confirm_page_carried(with_collection, fake_dspace) -> None:
    with_collection.post(
        "/review/42/deposit",
        data={
            TITLE: "泥沙研究",
            TITLE_LANG: "zh",
            "traditionalpageone::dc.date.issued": "2016",
            "traditionalpageone::dc.type": "Thesis",
            "license_granted": "true",
        },
    )

    title_ops = [
        op for patch in fake_dspace.patches for op in patch if op["path"].endswith("dc.title")
    ]
    assert title_ops[0]["value"] == [{"value": "泥沙研究", "language": "zh"}]
