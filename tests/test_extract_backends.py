"""Tier 1 backends: the wire shape, and where a document is allowed to go.

The security-relevant assertions here are the routing ones. `force_local` is a
promise to the submitter that an embargoed document is read on this machine and
nowhere else, and a promise like that is only as good as the test that a failure
of the local backend does not quietly resolve to the cloud.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from scholardrop.config import Instance
from scholardrop.extract import llm
from scholardrop.extract.backends import (
    AnthropicBackend,
    LlmUnavailable,
    OpenAICompatBackend,
    json_schema_for,
    local_is_configured,
    select_backend,
)
from scholardrop.extract.llm import LlmExtraction
from scholardrop.extract.models import LOW_CONFIDENCE, SourceTier
from scholardrop.extract.pipeline import extract_from_document
from scholardrop.extract.reader import PdfDocument
from scholardrop.settings import load_settings
from tests.test_extract_llm import FULL_EXTRACTION, FakeClient, response

OLLAMA = "http://127.0.0.1:11434"

ANSWER = {
    "title": "Sediment Transport in Ephemeral Prairie Streams",
    "authors": ["Okonkwo-Bell, Margaret"],
    "advisor": "Vasquez, Helen",
    "committee_members": ["Adeyemi, Samuel"],
    "keywords": ["sediment transport"],
    "abstract": "Ephemeral streams across the southern Great Plains move a great deal.",
    "date_issued": "2019",
    "degree": "Doctor of Philosophy",
    "department": "School of Geosciences",
    "language": "en",
    "doc_type": "dissertation",
}


def settings(**overrides: Any):
    return load_settings(secret_key="x", **overrides)


def instance(extraction: str = "cloud") -> Instance:
    return Instance(
        key="shareok",
        name="SHAREOK",
        base_url="https://shareok.org/server",
        mapping_profile="shareok-etd",
        extraction=extraction,  # type: ignore[arg-type]
    )


def document(text: str = "A dissertation submitted to the faculty.") -> PdfDocument:
    return PdfDocument(page_count=120, front_text=text)


def ollama_reply(content: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"message": {"role": "assistant", "content": json.dumps(content)}}
    )


def openai_reply(content: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}]}
    )


# --- the wire ------------------------------------------------------------


@respx.mock
async def test_ollama_passes_the_schema_in_format() -> None:
    """Constrained decoding, not a polite request: the schema rides in `format`."""
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=ollama_reply(ANSWER))
    backend = OpenAICompatBackend(OLLAMA, "qwen3:4b-instruct")

    result = await backend.extract(system="sys", prompt="body", schema=LlmExtraction)

    assert result["title"] == ANSWER["title"]
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "qwen3:4b-instruct"
    assert sent["stream"] is False
    assert sent["format"] == json_schema_for(LlmExtraction)
    assert sent["format"]["properties"]["committee_members"]["type"] == "array"
    assert sent["options"]["temperature"] == 0
    # Ollama truncates the *tail* of an over-long prompt, which is where an ETD's
    # signature page is. The context has to hold the whole trimmed prompt.
    assert sent["options"]["num_ctx"] >= 8192
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]


@respx.mock
async def test_openai_dialect_uses_response_format() -> None:
    route = respx.post(f"{OLLAMA}/v1/chat/completions").mock(return_value=openai_reply(ANSWER))
    backend = OpenAICompatBackend(OLLAMA, "qwen3-4b", dialect="openai")

    await backend.extract(system="sys", prompt="body", schema=LlmExtraction)

    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] == json_schema_for(LlmExtraction)
    assert "format" not in sent


def test_every_field_is_required_in_the_schema() -> None:
    """A decoder allowed to omit fields omits the ones it is unsure about."""
    schema = json_schema_for(LlmExtraction)
    assert set(schema["required"]) == set(LlmExtraction.model_fields)
    assert schema["additionalProperties"] is False


@respx.mock
async def test_unreachable_server_is_unavailable_not_a_crash() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    backend = OpenAICompatBackend(OLLAMA, "qwen3:4b-instruct")

    with pytest.raises(LlmUnavailable, match="did not answer"):
        await backend.extract(system="s", prompt="p", schema=LlmExtraction)


@respx.mock
async def test_non_json_output_is_rejected_rather_than_half_read() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "Sure! Here you go:"}})
    )
    backend = OpenAICompatBackend(OLLAMA, "qwen3:4b-instruct")

    with pytest.raises(LlmUnavailable, match="not JSON"):
        await backend.extract(system="s", prompt="p", schema=LlmExtraction)


@respx.mock
async def test_http_error_is_raised_for_the_retry_policy_to_judge() -> None:
    """A 404 (model not pulled) must reach llm._is_retryable, which says no."""
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(404, json={"error": "model not found"})
    )
    backend = OpenAICompatBackend(OLLAMA, "qwen3:4b-instruct")

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await backend.extract(system="s", prompt="p", schema=LlmExtraction)
    assert llm._is_retryable(caught.value) is False


def test_backend_without_a_model_refuses_to_exist() -> None:
    with pytest.raises(LlmUnavailable, match="no local model"):
        OpenAICompatBackend(OLLAMA, "")


# --- llm.extract over a local backend ------------------------------------


@respx.mock
async def test_local_extraction_produces_metadata_at_a_lower_confidence() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=ollama_reply(ANSWER))
    backend = OpenAICompatBackend(OLLAMA, "qwen3:4b-instruct")

    result = await llm.extract(document(), settings(), backend=backend)

    assert result.tier_used is SourceTier.LLM
    assert result.title.value == ANSWER["title"]
    # A 4B model read it, so the field is not "guessed" -- but the review form
    # should not present it as Claude's reading either.
    assert LOW_CONFIDENCE < result.title.confidence < llm.LLM_CONFIDENCE


@respx.mock
async def test_local_prompt_is_trimmed_to_the_context_budget() -> None:
    """Two prompts, one document: the local one has to fit in 8k of context."""
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=ollama_reply(ANSWER))
    big = PdfDocument(
        page_count=300,
        front_text="TITLE PAGE\n" + ("filler paragraph. " * 4000),
        back_text="Thesis Committee\nDr. Helen Vasquez, Chair\n",
    )
    config = settings(local_llm_max_chars=6000)

    await llm.extract(big, config, backend=OpenAICompatBackend(OLLAMA, "qwen3:4b-instruct"))

    sent = json.loads(route.calls.last.request.content)
    prompt = sent["messages"][1]["content"]
    assert len(prompt) < 6000 + 500  # budget plus the framing
    assert "TITLE PAGE" in prompt, "the title page is the point of the budget"
    assert "Dr. Helen Vasquez, Chair" in prompt, "so is the signature page in the tail"
    # The small-model system prompt, not the five-paragraph one.
    assert sent["messages"][0]["content"] == llm.COMPACT_SYSTEM_PROMPT


@respx.mock
async def test_off_schema_output_falls_back_rather_than_half_filling_a_form() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=ollama_reply({"title": 42, "authors": "not a list"})
    )

    with pytest.raises(LlmUnavailable, match="off-schema"):
        await llm.extract(document(), settings(), backend=OpenAICompatBackend(OLLAMA, "q"))


@respx.mock
async def test_warm_loads_the_model() -> None:
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=ollama_reply(ANSWER))

    await llm.warm_local(settings())

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["options"]["num_predict"] == 1


# --- routing: where the document is allowed to go ------------------------


def test_cloud_is_the_default_backend() -> None:
    backend = select_backend(settings(anthropic_api_key="k"), mode="cloud")
    assert isinstance(backend, AnthropicBackend)
    assert backend.local is False


def test_local_mode_never_reaches_the_cloud() -> None:
    backend = select_backend(settings(anthropic_api_key="k"), mode="local")
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.local is True


def test_force_local_overrides_a_cloud_instance() -> None:
    """The restricted-content checkbox beats the instance's own default."""
    backend = select_backend(settings(anthropic_api_key="k"), mode="cloud", force_local=True)
    assert isinstance(backend, OpenAICompatBackend)


def test_force_local_ignores_an_offered_cloud_client() -> None:
    backend = select_backend(settings(), mode="cloud", force_local=True, client=FakeClient())
    assert isinstance(backend, OpenAICompatBackend)


def test_force_local_without_a_local_model_is_unavailable_not_cloud() -> None:
    config = settings(anthropic_api_key="k", local_llm_model="")
    assert local_is_configured(config) is False

    with pytest.raises(LlmUnavailable):
        select_backend(config, mode="cloud", force_local=True)


async def test_restricted_submission_falls_back_to_heuristics_not_the_cloud() -> None:
    """The whole point of Phase 3b, end to end through the pipeline."""
    client = FakeClient(response(FULL_EXTRACTION))
    config = settings(anthropic_api_key="k", local_llm_model="")

    result = await extract_from_document(
        document("A dissertation submitted to the faculty.\n\nby\n\nJane Roe\n"),
        instance=instance("cloud"),
        settings=config,
        client=client,
        force_local=True,
    )

    assert client.messages.calls == [], "an embargoed document must not reach the API"
    assert result.tier_used in {SourceTier.HEURISTIC, SourceTier.NONE}
    assert "no local model configured" in (result.fallback_reason or "")


@respx.mock
async def test_restricted_submission_uses_the_local_model() -> None:
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=ollama_reply(ANSWER))
    client = FakeClient(response(FULL_EXTRACTION))

    result = await extract_from_document(
        document(),
        instance=instance("cloud"),
        settings=settings(anthropic_api_key="k"),
        client=client,
        force_local=True,
    )

    assert route.called
    assert client.messages.calls == []
    assert result.tier_used is SourceTier.LLM
    assert result.title.value == ANSWER["title"]


@respx.mock
async def test_local_server_down_on_a_restricted_submission_stays_local() -> None:
    """A local backend that is down is not a reason to send the text out."""
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    client = FakeClient(response(FULL_EXTRACTION))

    result = await extract_from_document(
        document("A dissertation submitted to the faculty.\n\nby\n\nJane Roe\n"),
        instance=instance("cloud"),
        settings=settings(anthropic_api_key="k"),
        client=client,
        force_local=True,
    )

    assert client.messages.calls == []
    assert result.tier_used in {SourceTier.HEURISTIC, SourceTier.NONE}
    assert "did not answer" in (result.fallback_reason or "")


async def test_heuristic_instance_is_not_reopened_by_the_checkbox() -> None:
    """`force_local` narrows where a document may go; it never widens it."""
    result = await extract_from_document(
        document("A dissertation submitted to the faculty.\n\nby\n\nJane Roe\n"),
        instance=instance("heuristic"),
        settings=settings(anthropic_api_key="k"),
        force_local=True,
    )

    assert result.tier_used in {SourceTier.HEURISTIC, SourceTier.NONE}


def test_legacy_llm_mode_still_parses_as_cloud() -> None:
    """instances.yaml written before Phase 3b must keep working."""
    assert instance("llm").extraction == "cloud"
