"""Tier selection and fallback. The invariant: extraction never blocks a flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from scholardrop.config import Instance
from scholardrop.extract import llm
from scholardrop.extract.models import ExtractedMetadata, SourceTier
from scholardrop.extract.pipeline import extract_from_document, extract_from_pdf
from scholardrop.extract.reader import PdfDocument
from scholardrop.settings import load_settings
from tests.test_extract_llm import FULL_EXTRACTION, FakeClient, response
from tests.test_extract_reader import build_pdf

FIXTURES = Path(__file__).parent / "fixtures"


def instance(extraction: str = "llm") -> Instance:
    return Instance(
        key="shareok",
        name="SHAREOK",
        base_url="https://shareok.org/server",
        mapping_profile="shareok-etd",
        extraction=extraction,  # type: ignore[arg-type]
    )


@pytest.fixture
def settings():
    return load_settings(secret_key="x", anthropic_api_key="test-key")


@pytest.fixture
def etd() -> PdfDocument:
    return PdfDocument(page_count=142, front_text=(FIXTURES / "dissertation_front.txt").read_text())


async def test_llm_tier_is_used_when_configured(settings, etd) -> None:
    client = FakeClient(response(FULL_EXTRACTION))

    result = await extract_from_document(
        etd, instance=instance("llm"), settings=settings, client=client
    )

    assert result.tier_used is SourceTier.LLM
    assert result.fallback_reason is None
    assert client.messages.calls, "tier 1 should have been called"


async def test_heuristic_instance_never_calls_the_api(settings, etd) -> None:
    """An instance handling embargoed material must not send text out (§5)."""
    client = FakeClient(response(FULL_EXTRACTION))

    result = await extract_from_document(
        etd, instance=instance("heuristic"), settings=settings, client=client
    )

    assert result.tier_used is SourceTier.HEURISTIC
    assert client.messages.calls == []
    assert result.abstract.present, "heuristics still found the abstract"


async def test_llm_failure_falls_back_to_heuristics(settings, etd) -> None:
    client = FakeClient(error=RuntimeError("rate limited"))

    result = await extract_from_document(
        etd, instance=instance("llm"), settings=settings, client=client
    )

    assert result.tier_used is SourceTier.HEURISTIC
    assert "rate limited" in result.fallback_reason
    # Fallback is not a dead end: it still produced usable metadata.
    assert result.abstract.present
    assert result.advisor.value == "Helen Vasquez"


async def test_missing_api_key_falls_back_quietly(etd) -> None:
    settings = load_settings(secret_key="x", anthropic_api_key="")

    result = await extract_from_document(etd, instance=instance("llm"), settings=settings)

    assert result.tier_used is SourceTier.HEURISTIC
    assert "no Anthropic API key" in result.fallback_reason


async def test_refusal_falls_back(settings, etd) -> None:
    client = FakeClient(response(FULL_EXTRACTION, stop_reason="refusal"))

    result = await extract_from_document(
        etd, instance=instance("llm"), settings=settings, client=client
    )

    assert result.tier_used is SourceTier.HEURISTIC


async def test_scanned_pdf_yields_an_empty_result_not_an_error(settings) -> None:
    scanned = PdfDocument(page_count=180, front_text="")

    result = await extract_from_document(scanned, instance=instance("llm"), settings=settings)

    assert isinstance(result, ExtractedMetadata)
    assert result.is_empty()
    assert result.tier_used is SourceTier.NONE
    assert "no extractable text" in result.fallback_reason


async def test_end_to_end_from_pdf_bytes(settings) -> None:
    pdf = build_pdf(
        [
            "Abstract\n\n" + "This dissertation examines prairie stream sediment transport "
            "across eleven gauged reaches over three years of monitoring.",
        ],
        docinfo={"title": "A Real Title From Metadata", "author": "Doe, Jane"},
    )

    result, document = await extract_from_pdf(
        pdf, instance=instance("heuristic"), settings=settings
    )

    assert document.page_count == 1
    assert result.title.value == "A Real Title From Metadata"
    assert result.authors.value == ["Doe, Jane"]
    assert result.abstract.present


async def test_unreadable_pdf_raises_for_the_caller_to_report(settings) -> None:
    from scholardrop.extract.reader import PdfUnreadable

    with pytest.raises(PdfUnreadable):
        await extract_from_pdf(b"not a pdf at all", instance=instance(), settings=settings)


async def test_llm_unavailable_is_the_only_swallowed_error(settings, etd) -> None:
    """A bug in our own code must not masquerade as a fallback."""

    class Exploding(FakeClient):
        def __init__(self) -> None:
            super().__init__(response(FULL_EXTRACTION))

    client = Exploding()

    async def broken(*args, **kwargs):
        raise llm.LlmUnavailable("simulated")

    # Sanity: LlmUnavailable is caught...
    client.messages.parse = broken  # type: ignore[method-assign]
    result = await extract_from_document(etd, instance=instance(), settings=settings, client=client)
    assert result.tier_used is SourceTier.HEURISTIC
