"""OpenAI provider adapter implementation."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator

import httpx
from pydantic import SecretStr

from velune.core.errors.provider import ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.adapters._http_errors import raise_typed_http_error
from velune.providers.adapters._live_catalog import get_json, reconcile
from velune.providers.adapters._messages import openai_messages
from velune.providers.adapters._toolcalls import (
    OpenAIStreamToolAccumulator,
    attach_openai_tools,
    parse_openai_tool_calls,
)
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key

_REASONING_MODEL = re.compile(r"^(o\d|gpt-5)", re.IGNORECASE)


def is_openai_reasoning_model(model_id: str) -> bool:
    """True for OpenAI models that use ``max_completion_tokens`` and fixed sampling."""
    mid = model_id.lower()
    return bool(_REASONING_MODEL.match(mid)) and "chat" not in mid


_NON_CHAT_MARKERS = (
    "audio", "realtime", "image", "tts", "transcribe", "embedding", "moderation",
    "search", "instruct", "davinci", "babbage", "whisper", "dall-e", "sora", "computer-use",
)  # fmt: skip
_CHAT_PREFIX = re.compile(r"^(gpt-|o\d|chatgpt-)", re.IGNORECASE)
# (prefix, context window) - first match wins, so order specific before general.
_CONTEXT_WINDOWS = (
    ("gpt-5", 400000),
    ("gpt-4.1", 1047576),
    ("o1", 200000),
    ("o3", 200000),
    ("o4", 200000),
    ("gpt-4o", 128000),
    ("chatgpt-4o", 128000),
    ("gpt-4-turbo", 128000),
    ("gpt-4-32k", 32768),
    ("gpt-4", 8192),
)


def is_openai_chat_model(model_id: str) -> bool:
    """True for chat-completions models; False for audio/image/embedding/etc."""
    mid = model_id.lower()
    return bool(_CHAT_PREFIX.match(mid)) and not any(m in mid for m in _NON_CHAT_MARKERS)


def openai_context_window(model_id: str) -> int:
    """Best-known context window (the /models endpoint doesn't report one)."""
    mid = model_id.lower()
    for prefix, window in _CONTEXT_WINDOWS:
        if mid.startswith(prefix):
            return window
    return 16385


async def fetch_live_models(api_key: str | None, base_url: str) -> dict[str, int | None] | None:
    """Return ``{model_id: None}`` for models the key can use, or None on failure."""
    if not api_key:
        return None
    data = await get_json(f"{base_url}/models", {"Authorization": f"Bearer {api_key}"})
    if not isinstance(data, dict):
        return None
    return {m["id"]: None for m in data.get("data", []) if isinstance(m, dict) and "id" in m}


def _curated(model_id: str, name: str, window: int, level: CapabilityLevel, cost: float):
    return ModelDescriptor(
        model_id=model_id,
        display_name=name,
        provider_id="openai",
        context_length=window,
        cost_per_1k_tokens=cost,
        capabilities={
            "coding": level,
            "reasoning": level,
            "planning": level,
            "summarization": level,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.EXPERT,
            "long_context": level,
        },
        is_local=False,
    )


OPENAI_MODELS: list[ModelDescriptor] = [
    _curated("gpt-4o", "GPT-4o", 128000, CapabilityLevel.EXPERT, 0.005),
    _curated("gpt-4o-mini", "GPT-4o Mini", 128000, CapabilityLevel.ADVANCED, 0.00015),
]


class OpenAIProvider(ModelProvider):
    """OpenAI provider for GPT chat and embedding models."""

    # stream() accumulates delta.tool_calls fragments and emits a final
    # tool-call chunk — the tool loop may stream turns with tools attached.
    # Inherited by the Groq/OpenRouter adapters, which share this stream().
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(
        self, api_key: str | SecretStr | None = None, base_url: str = "https://api.openai.com/v1"
    ) -> None:
        self._api_key = api_key or get_key("openai")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=True,
            max_context_window=128000,
        )

    @property
    def provider_id(self) -> str:
        return "openai"

    async def initialize(self) -> None:
        """Initialize headers and async client connection."""
        if not self._api_key:
            raise ProviderAuthenticationError(
                "OpenAI API key not found in configuration or environment"
            )
        if not self.client:
            headers = {"Authorization": f"Bearer {self._api_key}"}
            self.client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=300.0)

    def _raise_provider_error(self, exc: httpx.HTTPError, action: str) -> None:
        """Map an httpx failure to the right typed error.

        Each branch is a *deterministic* verdict — about the key, the rate
        limit, or the request itself — and is deliberately excluded from
        :data:`velune.providers.retrying.RETRYABLE_EXCEPTIONS`: retrying an
        unknown/decommissioned model id or a malformed request three times in
        a row just delays the same inevitable failure. Only the fallback
        :class:`InferenceError` at the bottom (5xx, network hiccups, anything
        unclassified) is retryable. Shared by every OpenAI-compatible
        subclass (Groq, OpenRouter, Together, Fireworks, xAI, Meta).
        """
        raise_typed_http_error(self.provider_id, exc, action)

    async def list_models(self) -> list[ModelDescriptor]:
        """Return the OpenAI lineup, reconciled with the models the account can use."""
        await self.initialize()
        if self.provider_id != "openai":
            return list(OPENAI_MODELS)
        return reconcile(
            OPENAI_MODELS,
            await fetch_live_models(self._api_key, self._base_url),
            "openai",
            accept=is_openai_chat_model,
            fallback_window=openai_context_window,
            is_strong=lambda mid: openai_context_window(mid) >= 128000,
        )

    def _chat_payload(self, request: InferenceRequest) -> dict:
        """Build the /chat/completions body for *request*.

        OpenAI's reasoning families (o-series, gpt-5) reject ``max_tokens`` and
        any non-default sampling parameter with an HTTP 400 — they take
        ``max_completion_tokens`` and fix temperature/top_p themselves. Other
        OpenAI-compatible providers (Groq, xAI, ...) subclass this adapter and
        keep the classic fields.
        """
        payload: dict = {"model": request.model_id, "messages": openai_messages(request.messages)}
        if self.provider_id == "openai" and is_openai_reasoning_model(request.model_id):
            payload["max_completion_tokens"] = request.max_tokens
        else:
            payload["temperature"] = request.temperature
            payload["max_tokens"] = request.max_tokens
            payload["top_p"] = request.top_p
        if request.stop_sequences and not (
            self.provider_id == "openai" and is_openai_reasoning_model(request.model_id)
        ):
            payload["stop"] = request.stop_sequences
        return payload

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Standard chat inference."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            payload = self._chat_payload(request)
            attach_openai_tools(payload, request)

            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            message = data["choices"][0]["message"]
            tool_calls = parse_openai_tool_calls(message)
            usage = data.get("usage", {})
            return InferenceResponse(
                # content is null on pure tool-call turns
                content=message.get("content") or "",
                model_id=request.model_id,
                finish_reason=(
                    "tool_calls" if tool_calls else (data["choices"][0]["finish_reason"] or "stop")
                ),
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=latency,
                tool_calls=tool_calls,
            )
        except httpx.HTTPError as e:
            self._raise_provider_error(e, "completion")
            raise  # unreachable — _raise_provider_error always raises

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Streaming chat completions.

        When the request carries tools, ``delta.tool_calls`` fragments are
        accumulated and surfaced once complete on a final chunk with
        ``finish_reason="tool_calls"`` and ``metadata["tool_calls"]`` set to
        the normalized :class:`ToolCall` list.
        """
        await self.initialize()
        assert self.client is not None
        accumulator = OpenAIStreamToolAccumulator()
        try:
            payload = self._chat_payload(request)
            payload["stream"] = True
            attach_openai_tools(payload, request)

            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    # Read the error body so the typed error can say *why*.
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0]["delta"]
                            accumulator.add(delta.get("tool_calls"))
                            yield StreamChunk(
                                content=delta.get("content") or "",
                                finish_reason=data["choices"][0].get("finish_reason"),
                            )
                        except (json.JSONDecodeError, KeyError):
                            continue

            tool_calls = accumulator.finalize()
            if tool_calls:
                yield StreamChunk(
                    content="",
                    finish_reason="tool_calls",
                    metadata={"tool_calls": tool_calls},
                )
        except httpx.HTTPError as e:
            self._raise_provider_error(e, "stream")
            raise  # unreachable — _raise_provider_error always raises

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Generate batch embeddings."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.post(
                "/embeddings", json={"model": model_id, "input": texts}
            )
            response.raise_for_status()
            data = response.json()
            # Sort by index to maintain token alignments
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in sorted_data]
        except httpx.HTTPError as e:
            self._raise_provider_error(e, "embedding")
            raise  # unreachable — _raise_provider_error always raises

    async def health_check(self) -> ProviderHealth:
        """Verifies API credentials and connectivity."""
        try:
            await self.initialize()
            assert self.client is not None
            resp = await self.client.get("/models")
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
