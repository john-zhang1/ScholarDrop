"""Tier 1 backends: where the model runs (DESIGN.md §5, "Tier 1 backends").

Tier 1 is a *task*, not a vendor. `llm.py` owns the schema, the prompt, and the
retry policy; a backend owns nothing but "send this system+user turn, come back
with an object that validates against this schema".

Two implementations:

- `AnthropicBackend` — the cloud path, unchanged behaviour: `messages.parse` with
  a Pydantic output schema.
- `OpenAICompatBackend` — anything serving a local model: Ollama, vLLM,
  llama.cpp's server, LM Studio. This is the embargo path. Document text goes to
  `127.0.0.1` and nowhere else.

Both enforce the schema **server-side**, by constrained decoding rather than by
asking politely, which is what makes a 4B model usable here at all: the failure
mode stops being "unparseable output" and becomes "a field is wrong", and a wrong
field is what the confidence flags and the human review step already handle.

Two wire dialects, because the schema is passed differently and the difference is
not cosmetic -- get it wrong and the server free-runs:

- `ollama`  -> POST {base}/api/chat with the schema in `format` (Ollama's native
  endpoint; its OpenAI-compatible one is fine too, but `format` is where its
  constrained decoding is documented to live).
- `openai`  -> POST {base}/v1/chat/completions with the schema in
  `response_format.json_schema` (vLLM, llama.cpp, LM Studio).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")

Dialect = Literal["ollama", "openai"]


class LlmUnavailable(Exception):
    """No backend, or the backend could not be used. Caller falls back to tier 2.

    Lives here rather than in llm.py because a backend is what discovers it, and
    llm.py re-exports it -- `llm.LlmUnavailable` is the name the pipeline catches.
    """


class LLMBackend(Protocol):
    """Send one extraction turn; return the raw object the model produced."""

    name: str
    label: str
    #: True when this backend keeps the document on this machine (DESIGN.md §5).
    local: bool

    async def extract(
        self, *, system: str, prompt: str, schema: type[BaseModel]
    ) -> dict[str, Any]: ...

    async def warm(self) -> None:
        """Pay any first-call cost (grammar compile, model load) up front."""


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """A JSON Schema a constrained decoder will accept.

    Pydantic marks a field optional when it has a default, but a decoder that may
    omit fields is a decoder that omits the ones it is unsure about -- which is
    the model quietly choosing what to answer. Every field is required; "absent"
    is spelled `null` or `[]`, which the prompt already asks for.
    """
    document = schema.model_json_schema()
    properties = document.get("properties", {})
    document["required"] = list(properties)
    document["additionalProperties"] = False
    return document


class AnthropicBackend:
    """Claude via the SDK's structured outputs. The default and the accurate one."""

    name = "anthropic"
    local = False

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self.label = f"anthropic/{settings.anthropic_model}"

    async def extract(self, *, system: str, prompt: str, schema: type[BaseModel]) -> dict[str, Any]:
        response = await self._api().messages.parse(
            model=self._settings.anthropic_model,
            max_tokens=MAX_TOKENS,
            system=system,
            output_config={"effort": self._settings.anthropic_effort},
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise LlmUnavailable("the model declined to process this document")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LlmUnavailable("response contained no parsed output")

        usage = getattr(response, "usage", None)
        log.info(
            "tier 1 extraction ok (backend=anthropic, model=%s, in=%s, out=%s)",
            getattr(response, "model", self._settings.anthropic_model),
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )
        return parsed.model_dump()

    async def warm(self) -> None:
        """Compile the output schema's grammar before a submission needs it.

        The first `parse` call with a given schema pays for compiling its grammar
        server-side. Observed on `LlmExtraction`: ~105s cold, and a 400 "Grammar
        compilation timed out" when it does not make it, against ~17s once
        cached. A submission should never be the request that discovers this.
        """
        from scholardrop.extract.llm import SYSTEM_PROMPT, WARM_PROMPT, LlmExtraction

        await self._api().messages.parse(
            model=self._settings.anthropic_model,
            max_tokens=WARM_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={"effort": self._settings.anthropic_effort},
            messages=[{"role": "user", "content": WARM_PROMPT}],
            output_format=LlmExtraction,
        )

    def _api(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._settings.anthropic_api_key:
            raise LlmUnavailable("no Anthropic API key configured")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LlmUnavailable(f"anthropic SDK unavailable: {exc}") from exc
        self._client = AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        return self._client


class OpenAICompatBackend:
    """A model served on this box (or any OpenAI-compatible server).

    Zero egress is the point, so the transport is deliberately dumb: one POST, no
    SDK, no telemetry, no retry inside the backend. A server that is not there
    raises `LlmUnavailable` immediately rather than making an embargoed
    submission wait out two timeouts -- the caller falls back to heuristics,
    which is the only other thing allowed to touch this document.
    """

    name = "local"
    local = True

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        dialect: Dialect = "ollama",
        timeout_seconds: float = 300.0,
        num_ctx: int = 8192,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not model:
            raise LlmUnavailable("no local model configured")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dialect: Dialect = dialect
        self.timeout_seconds = timeout_seconds
        self.num_ctx = num_ctx
        self.label = f"{dialect}/{model}"
        self._transport = transport

    async def extract(self, *, system: str, prompt: str, schema: type[BaseModel]) -> dict[str, Any]:
        url, payload = self._request(system, prompt, schema)
        content = await self._post(url, payload)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            # Constrained decoding should make this impossible; if a server was
            # configured to ignore the schema, say so rather than half-parsing.
            raise LlmUnavailable(f"{self.label} returned output that is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LlmUnavailable(f"{self.label} returned {type(data).__name__}, expected an object")
        return data

    async def warm(self) -> None:
        """Load the model into RAM. On a cold Ollama this is most of the latency."""
        url = f"{self.base_url}/api/chat" if self.dialect == "ollama" else None
        if url is None:  # nothing standard to poke on an OpenAI-compatible server
            return
        await self._post(
            url,
            {
                "model": self.model,
                "messages": [{"role": "user", "content": "ok"}],
                "stream": False,
                "options": {"num_predict": 1},
            },
        )

    def _request(
        self, system: str, prompt: str, schema: type[BaseModel]
    ) -> tuple[str, dict[str, Any]]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        json_schema = json_schema_for(schema)
        if self.dialect == "ollama":
            return (
                f"{self.base_url}/api/chat",
                {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    # Constrained decoding: the grammar comes from this schema.
                    "format": json_schema,
                    # Deterministic, and a context big enough for the trimmed
                    # prompt -- Ollama's default 4k silently drops the tail of a
                    # longer one, which is exactly where the committee page is.
                    "options": {"temperature": 0, "num_ctx": self.num_ctx},
                },
            )
        return (
            f"{self.base_url}/v1/chat/completions",
            {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "strict": True,
                        "schema": json_schema,
                    },
                },
            },
        )

    async def _post(self, url: str, payload: dict[str, Any]) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"{self.label} at {self.base_url} did not answer: {exc}") from exc

        if response.status_code >= 400:
            # Raised, not wrapped: llm._is_retryable decides whether asking a
            # busy server again is worth it. A 404 (model not pulled) is not.
            response.raise_for_status()
        return _content_of(response.json(), self.dialect, self.label)


def _content_of(body: Any, dialect: Dialect, label: str) -> str:
    """Pull the assistant text out of whichever response shape came back."""
    if not isinstance(body, dict):
        raise LlmUnavailable(f"{label} returned an unexpected response")
    if dialect == "ollama":
        content = (body.get("message") or {}).get("content")
    else:
        choices = body.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
    if not content:
        raise LlmUnavailable(f"{label} returned an empty response")
    return content


MAX_TOKENS = 4096
# The warm-up call only exists to compile the grammar / load the weights; it needs
# room for a schema-shaped answer of nulls and nothing more.
WARM_MAX_TOKENS = 256


def select_backend(
    settings: Settings,
    *,
    mode: str = "cloud",
    force_local: bool = False,
    client: Any | None = None,
) -> LLMBackend:
    """Pick the backend for one extraction.

    `mode` is the instance's `extraction:` setting; `force_local` is the
    per-submission "restricted content" checkbox. Forcing local is a promise
    about where the bytes go, so it can only ever resolve to the local backend or
    to `LlmUnavailable` (whereupon the caller runs heuristics) -- never to the
    cloud, whatever the instance is configured for.
    """
    if force_local or mode == "local":
        return local_backend(settings)
    if client is not None:
        return AnthropicBackend(settings, client=client)
    return AnthropicBackend(settings)


def local_backend(settings: Settings) -> OpenAICompatBackend:
    """The local backend, or `LlmUnavailable` if this deployment has none."""
    return OpenAICompatBackend(
        settings.local_llm_base_url,
        settings.local_llm_model,
        dialect=settings.local_llm_dialect,
        timeout_seconds=settings.local_llm_timeout_seconds,
        num_ctx=settings.local_llm_num_ctx,
    )


def local_is_configured(settings: Settings) -> bool:
    return bool(settings.local_llm_base_url and settings.local_llm_model)
