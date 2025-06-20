"""Tier 1 extraction with a mocked Anthropic client.

No API credentials are available in this environment, so these tests pin the
request shape, the schema, and every failure path -- but they cannot tell us how
well the prompt actually performs. That is what the Phase 3 gate run is for.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scholardrop.extract import llm
from scholardrop.extract.llm import LlmExtraction, LlmUnavailable
from scholardrop.extract.models import LOW_CONFIDENCE, SourceTier
from scholardrop.extract.reader import PdfDocument
from scholardrop.settings import load_settings

FULL_EXTRACTION = LlmExtraction(
    title="Sediment Transport in Ephemeral Prairie Streams",
    authors=["Okonkwo-Bell, Margaret"],
    advisor="Vasquez, Helen",
    committee_members=["Adeyemi, Samuel", "Raghunathan, Priya"],
    keywords=["sediment transport", "ephemeral streams"],
    abstract="Ephemeral streams across the southern Great Plains move a disproportionate share.",
    date_issued="2019",
    degree="Doctor of Philosophy",
    department="School of Geosciences",
    language="en",
    doc_type="dissertation",
)


class FakeMessages:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(result, error)


def response(parsed: Any, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=4200, output_tokens=380),
    )


@pytest.fixture
def settings():
    return load_settings(secret_key="x", anthropic_api_key="test-key")


@pytest.fixture
def document() -> PdfDocument:
    return PdfDocument(
        page_count=142,
        front_text="SEDIMENT TRANSPORT IN EPHEMERAL PRAIRIE STREAMS\n\nBy Margaret Okonkwo-Bell",
        back_text="Appendix C",
        docinfo={"title": "thesis_final_v3.docx", "author": "mokonkwo"},
    )


# ---- the happy path -------------------------------------------------------


async def test_extracts_every_field(settings, document) -> None:
    client = FakeClient(response(FULL_EXTRACTION))

    result = await llm.extract(document, settings, client=client)

    assert result.title.value == "Sediment Transport in Ephemeral Prairie Streams"
    assert result.authors.value == ["Okonkwo-Bell, Margaret"]
    assert result.advisor.value == "Vasquez, Helen"
    assert result.committee_members.value == ["Adeyemi, Samuel", "Raghunathan, Priya"]
    assert result.keywords.value == ["sediment transport", "ephemeral streams"]
    assert result.date_issued.value == "2019"
    assert result.doc_type.value == "dissertation"


async def test_tier1_values_are_not_flagged_for_review(settings, document) -> None:
    result = await llm.extract(document, settings, client=client_for(FULL_EXTRACTION))

    for name, extracted in result.present_fields().items():
        assert extracted.source_tier is SourceTier.LLM, name
        assert extracted.confidence >= LOW_CONFIDENCE, name
    assert result.tier_used is SourceTier.LLM
    assert result.low_confidence_fields() == []


def client_for(parsed: Any) -> FakeClient:
    return FakeClient(response(parsed))


# ---- the request ----------------------------------------------------------


async def test_request_uses_configured_model_and_forced_schema(settings, document) -> None:
    client = client_for(FULL_EXTRACTION)

    await llm.extract(document, settings, client=client)

    sent = client.messages.calls[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["output_format"] is LlmExtraction, "the schema must be forced, not requested"
    assert sent["output_config"] == {"effort": settings.anthropic_effort}
    assert sent["max_tokens"] == llm.MAX_TOKENS


async def test_prompt_carries_the_document_text_and_context(settings, document) -> None:
    client = client_for(FULL_EXTRACTION)

    await llm.extract(document, settings, client=client)

    sent = client.messages.calls[0]
    prompt = sent["messages"][0]["content"]
    assert "SEDIMENT TRANSPORT IN EPHEMERAL PRAIRIE STREAMS" in prompt
    assert "Appendix C" in prompt, "the tail is included"
    assert "<document_text>" in prompt
    assert "page count: 142" in prompt
    # DocInfo is passed as a hint, explicitly marked untrustworthy.
    assert "thesis_final_v3.docx" in prompt
    assert "may be wrong" in prompt


async def test_prompt_forbids_invention_and_paraphrase(settings, document) -> None:
    client = client_for(FULL_EXTRACTION)

    await llm.extract(document, settings, client=client)

    system = client.messages.calls[0]["system"].lower()
    assert "never infer" in system
    assert "verbatim" in system
    assert "do not summarise" in system
    assert "empty field is correct" in system


async def test_long_documents_are_truncated(settings) -> None:
    document = PdfDocument(page_count=900, front_text="x" * 200_000)
    client = client_for(FULL_EXTRACTION)

    await llm.extract(document, settings, client=client)

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert len(prompt) < llm.MAX_CHARS + 2000


# ---- schema handling ------------------------------------------------------


async def test_absent_fields_come_back_empty_not_invented(settings, document) -> None:
    sparse = LlmExtraction(title="An Article With Little Front Matter", authors=["Ferrari, Lucia"])

    result = await llm.extract(document, settings, client=client_for(sparse))

    assert result.title.present
    assert result.advisor.present is False
    assert result.committee_members.value == []
    assert result.keywords.value == []
    assert result.abstract.present is False


async def test_advisor_is_never_repeated_among_members(settings, document) -> None:
    """Rule 4 of the prompt, enforced rather than trusted."""
    sloppy = LlmExtraction(
        advisor="Vasquez, Helen",
        committee_members=["Vasquez, Helen", "Adeyemi, Samuel"],
    )

    result = await llm.extract(document, settings, client=client_for(sloppy))

    assert result.advisor.value == "Vasquez, Helen"
    assert result.committee_members.value == ["Adeyemi, Samuel"]


async def test_blank_strings_are_treated_as_absent(settings, document) -> None:
    padded = LlmExtraction(title="   ", authors=["", "  ", "Real Name"], abstract="\n")

    result = await llm.extract(document, settings, client=client_for(padded))

    assert result.title.present is False
    assert result.authors.value == ["Real Name"]
    assert result.abstract.present is False


def test_schema_matches_the_neutral_field_names() -> None:
    from scholardrop.extract.models import ExtractedMetadata

    assert set(LlmExtraction.model_fields) == set(ExtractedMetadata.field_names())


# ---- failure paths (all fall back to tier 2) ------------------------------


async def test_api_error_raises_llm_unavailable(settings, document) -> None:
    client = FakeClient(error=RuntimeError("connection reset"))

    with pytest.raises(LlmUnavailable, match="connection reset"):
        await llm.extract(document, settings, client=client)


async def test_refusal_raises_llm_unavailable(settings, document) -> None:
    client = FakeClient(response(FULL_EXTRACTION, stop_reason="refusal"))

    with pytest.raises(LlmUnavailable, match="declined"):
        await llm.extract(document, settings, client=client)


async def test_missing_parsed_output_raises_llm_unavailable(settings, document) -> None:
    client = FakeClient(response(None))

    with pytest.raises(LlmUnavailable, match="no parsed output"):
        await llm.extract(document, settings, client=client)


async def test_document_without_text_is_not_sent(settings) -> None:
    client = client_for(FULL_EXTRACTION)

    with pytest.raises(LlmUnavailable, match="no extractable text"):
        await llm.extract(PdfDocument(page_count=300), settings, client=client)

    assert client.messages.calls == [], "a scanned PDF must not be uploaded for nothing"


async def test_missing_api_key_raises_llm_unavailable(document) -> None:
    settings = load_settings(secret_key="x", anthropic_api_key="")

    with pytest.raises(LlmUnavailable, match="no Anthropic API key"):
        await llm.extract(document, settings)
