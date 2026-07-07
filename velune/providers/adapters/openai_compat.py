"""Generic OpenAI-compatible local provider adapter.

Serves models discovered on self-hosted OpenAI-compatible servers (vLLM,
LocalAI, llama.cpp ``server``, text-generation-webui, …) that expose the
standard ``/v1/chat/completions`` and ``/v1/models`` endpoints. No API key is
required; the only configuration is the ``base_url`` of the local server.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from velune.core.errors.provider import InferenceError, ProviderConnectionError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.adapters._toolcalls import (
    OpenAIStreamToolAccumulator,
    attach_openai_tools,
    parse_openai_tool_calls,
)
from velune.providers.base import ModelProvider


class OpenAICompatProvider(ModelProvider):
    """Provider for generic OpenAI-compatible local endpoints."""

    # stream() accumulates delta.tool_calls fragments (vLLM, LM Studio, and
    # llama.cpp server all speak the OpenAI streaming tool-call format).
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(self, base_url: str = "http://localhost:8000/v1") -> None:
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
        return "openai-compat"

    async def initialize(self) -> None:
        if not self.client:
            self.client = httpx.AsyncClient(base_url=self._base_url, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.get("/models")
            response.raise_for_status()
            data = response.json()
            descriptors: list[ModelDescriptor] = []
            for item in data.get("data", []):
                m_id = item.get("id")
                if not m_id:
                    continue
                descriptors.append(
                    ModelDescriptor(
                        model_id=m_id,
                        display_name=m_id,
                        provider_id="openai-compat",
                        context_length=8192,
                        capabilities={
                            "coding": CapabilityLevel.INTERMEDIATE,
                            "reasoning": CapabilityLevel.BASIC,
                            "planning": CapabilityLevel.BASIC,
                            "summarization": CapabilityLevel.BASIC,
                            "embedding": CapabilityLevel.BASIC,
                            "instruction_following": CapabilityLevel.INTERMEDIATE,
                            "multimodal": CapabilityLevel.NONE,
                            "tool_use": CapabilityLevel.BASIC,
                            "long_context": CapabilityLevel.BASIC,
                        },
                        is_local=True,
                        metadata={"base_url": self._base_url},
                    )
                )
            return descriptors
        except httpx.HTTPError as e:
            raise ProviderConnectionError(f"OpenAI-compatible server connection error: {e}")

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            payload = {
                "model": request.model_id,
                "messages": request.messages,
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

            message = data["choices"][0]["message"]
            tool_calls = parse_openai_tool_calls(message)
            return InferenceResponse(
                content=message.get("content") or "",
                model_id=request.model_id,
                finish_reason=(
                    "tool_calls"
                    if tool_calls
                    else (data["choices"][0].get("finish_reason") or "stop")
                ),
                tokens_used=data.get("usage", {}).get("total_tokens", 0),
                latency_ms=latency,
                tool_calls=tool_calls,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"OpenAI-compatible completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        await self.initialize()
        assert self.client is not None
        accumulator = OpenAIStreamToolAccumulator()
        try:
            payload = {
                "model": request.model_id,
                "messages": request.messages,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "top_p": request.top_p,
                "stream": True,
            }
            if request.stop_sequences:
                payload["stop"] = request.stop_sequences
            attach_openai_tools(payload, request)

            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
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
            raise InferenceError(f"OpenAI-compatible stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
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
            raise InferenceError(f"OpenAI-compatible embedding failed: {e}")

    async def health_check(self) -> ProviderHealth:
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
