"""LM Studio provider adapter implementation."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from velune.core.errors.provider import InferenceError, ProviderConnectionError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.adapters._messages import openai_messages
from velune.providers.adapters._toolcalls import (
    OpenAIStreamToolAccumulator,
    attach_openai_tools,
    parse_openai_tool_calls,
)
from velune.providers.base import ModelProvider

#: Finish reasons that mean the model finished its tool calls (some servers say "stop").
_COMPLETE_FINISH_REASONS = ("tool_calls", "stop", "function_call")


def _stream_error_text(data: dict) -> str:
    """The human-readable message of an error frame, whatever shape the server used."""
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error
    return str(error or data)[:300]


class LMStudioProvider(ModelProvider):
    """LM Studio provider for local OpenAI-compatible endpoints.

    LM Studio serves the OpenAI chat-completions format, including ``tools`` and
    streamed ``delta.tool_calls`` fragments (for models it loads with tool-use
    support), so tool calling uses the shared OpenAI wire helpers.
    """

    # stream() accumulates delta.tool_calls fragments and emits a final
    # metadata["tool_calls"] chunk, which the tool loop requires for streaming.
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(self, base_url: str = "http://localhost:1234/v1") -> None:
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=True,
            max_context_window=32768,
        )

    @property
    def provider_id(self) -> str:
        return "lmstudio"

    async def initialize(self) -> None:
        """Initialize headers and async client connection."""
        if not self.client:
            self.client = httpx.AsyncClient(base_url=self._base_url, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        """Fetch list of active models loaded in LM Studio."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.get("/models")
            response.raise_for_status()
            data = response.json()

            descriptors: list[ModelDescriptor] = []
            for item in data.get("data", []):
                m_id = item["id"]
                descriptors.append(
                    ModelDescriptor(
                        model_id=m_id,
                        display_name=m_id,
                        provider_id="lmstudio",
                        context_length=32768,
                        capabilities={
                            "coding": CapabilityLevel.INTERMEDIATE,
                            "reasoning": CapabilityLevel.INTERMEDIATE,
                            "planning": CapabilityLevel.BASIC,
                            "summarization": CapabilityLevel.INTERMEDIATE,
                            "embedding": CapabilityLevel.INTERMEDIATE,
                            "instruction_following": CapabilityLevel.INTERMEDIATE,
                            "multimodal": CapabilityLevel.NONE,
                            "tool_use": CapabilityLevel.INTERMEDIATE,
                            "long_context": CapabilityLevel.BASIC,
                        },
                        is_local=True,
                    )
                )
            return descriptors
        except httpx.HTTPError as e:
            raise ProviderConnectionError(f"LM Studio connection error: {e}")

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Standard chat inference."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            payload = {
                "model": request.model_id,
                "messages": openai_messages(request.messages),
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "top_p": request.top_p,
            }
            if request.stop_sequences:
                payload["stop"] = request.stop_sequences
            attach_openai_tools(payload, request)

            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            choice = data["choices"][0]
            message = choice["message"]
            tool_calls = parse_openai_tool_calls(message)
            return InferenceResponse(
                content=message.get("content") or "",
                model_id=request.model_id,
                finish_reason="tool_calls"
                if tool_calls
                else (choice.get("finish_reason") or "stop"),
                tokens_used=(data.get("usage") or {}).get("total_tokens", 0),
                latency_ms=latency,
                tool_calls=tool_calls,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"LM Studio completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Streaming chat completions."""
        await self.initialize()
        assert self.client is not None
        accumulator = OpenAIStreamToolAccumulator()
        try:
            payload = {
                "model": request.model_id,
                "messages": openai_messages(request.messages),
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "top_p": request.top_p,
                "stream": True,
            }
            if request.stop_sequences:
                payload["stop"] = request.stop_sequences
            attach_openai_tools(payload, request)

            finish_reason: str | None = None  # the last terminal reason the server sent
            done = False  # saw the ``[DONE]`` sentinel
            corrupt = False  # a data frame that could not be parsed
            error_event = False  # an ``event: error`` line announced the next frame is an error

            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        error_event = line[6:].strip().lower() == "error"
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        done = True
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        # Harmless in a text stream, but it may have carried a tool-call
                        # fragment: remember it and decide when the stream ends.
                        corrupt = True
                        continue
                    if not isinstance(data, dict):
                        corrupt = True
                        continue
                    # An error frame is a failure, not a chunk: never fall through to a
                    # "successful" finish that would publish half-built tool calls.
                    if error_event or data.get("error"):
                        raise InferenceError(f"LM Studio stream error: {_stream_error_text(data)}")
                    try:
                        choice = data["choices"][0]
                        delta = choice["delta"]
                    except (KeyError, IndexError, TypeError):
                        continue  # e.g. a usage-only frame with no choices
                    reason = choice.get("finish_reason")
                    if reason == "error":
                        raise InferenceError("LM Studio stream error: the server ended the reply")
                    if reason:
                        finish_reason = reason
                    accumulator.add(delta.get("tool_calls"))
                    yield StreamChunk(content=delta.get("content") or "", finish_reason=reason)

            # Past this point the stream ended without an error frame. It is only complete if
            # the server said so; a connection that just stopped is a failure, and must never
            # be turned into a "finished" reply or published tool calls.
            if not (done or finish_reason):
                raise InferenceError(
                    "LM Studio stream ended before the reply was complete "
                    "(no finish_reason and no [DONE])"
                )
            tool_calls = accumulator.finalize()
            if tool_calls:
                if corrupt:
                    raise InferenceError(
                        "LM Studio stream contained an unreadable frame while building tool "
                        "calls; refusing to run a possibly incomplete call"
                    )
                if finish_reason not in (None, *_COMPLETE_FINISH_REASONS):
                    raise InferenceError(
                        f"LM Studio stream stopped early ({finish_reason!r}) while building "
                        "tool calls; refusing to run an incomplete call"
                    )
                yield StreamChunk(
                    content="",
                    finish_reason="tool_calls",
                    metadata={"tool_calls": tool_calls},
                )
        except httpx.HTTPError as e:
            raise InferenceError(f"LM Studio stream failed: {e}")

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
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in sorted_data]
        except httpx.HTTPError as e:
            raise InferenceError(f"LM Studio embedding failed: {e}")

    async def health_check(self) -> ProviderHealth:
        """Verifies connectivity."""
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
