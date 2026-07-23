"""Mistral AI provider adapter."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx
from pydantic import SecretStr

from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.adapters._toolcalls import (
    OpenAIStreamToolAccumulator,
    attach_openai_tools,
    parse_openai_tool_calls,
)
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key


class MistralProvider(ModelProvider):
    """Mistral AI provider — La Plateforme REST API (OpenAI-compatible wire format)."""

    # La Plateforme's chat/completions endpoint speaks the same tool-call
    # wire format as OpenAI, including streamed delta.tool_calls fragments.
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        base_url: str = "https://api.mistral.ai/v1",
    ) -> None:
        self._api_key = api_key or get_key("mistral")
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
        return "mistral"

    async def initialize(self) -> None:
        if not self._api_key:
            raise ProviderAuthenticationError(
                "Mistral API key not found. Set MISTRAL_API_KEY or run `velune provider add mistral`."
            )
        if not self.client:
            self.client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=300.0,
            )

    async def list_models(self) -> list[ModelDescriptor]:
        await self.initialize()
        return [
            ModelDescriptor(
                model_id="mistral-large-latest",
                display_name="Mistral Large",
                provider_id="mistral",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.EXPERT,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.EXPERT,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                cost_per_1k_tokens=0.002,
            ),
            ModelDescriptor(
                model_id="mistral-small-latest",
                display_name="Mistral Small",
                provider_id="mistral",
                context_length=32000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.INTERMEDIATE,
                    "long_context": CapabilityLevel.INTERMEDIATE,
                },
                is_local=False,
                cost_per_1k_tokens=0.0002,
            ),
            ModelDescriptor(
                model_id="codestral-latest",
                display_name="Codestral",
                provider_id="mistral",
                context_length=32000,
                capabilities={
                    "coding": CapabilityLevel.EXPERT,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.INTERMEDIATE,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.INTERMEDIATE,
                    "long_context": CapabilityLevel.INTERMEDIATE,
                },
                is_local=False,
                cost_per_1k_tokens=0.0003,
                tags=["code"],
            ),
        ]

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
            attach_openai_tools(payload, request)
            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0
            usage = data.get("usage", {})
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
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=latency,
                tool_calls=tool_calls,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ProviderAuthenticationError("Mistral API key is invalid or expired.")
            raise InferenceError(f"Mistral completion failed: {e}")
        except httpx.HTTPError as e:
            raise InferenceError(f"Mistral completion failed: {e}")

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
            raise InferenceError(f"Mistral stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.post(
                "/embeddings",
                json={"model": model_id or "mistral-embed", "input": texts},
            )
            response.raise_for_status()
            data = response.json()
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in sorted_data]
        except httpx.HTTPError as e:
            raise InferenceError(f"Mistral embedding failed: {e}")

    async def health_check(self) -> ProviderHealth:
        try:
            await self.initialize()
            assert self.client is not None
            resp = await self.client.get("/models")
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            if resp.status_code == 401:
                return ProviderHealth.UNAVAILABLE
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
