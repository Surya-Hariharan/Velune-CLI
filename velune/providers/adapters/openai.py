"""OpenAI provider adapter implementation."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx
from pydantic import SecretStr

from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
)
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key


class OpenAIProvider(ModelProvider):
    """OpenAI provider for GPT chat and embedding models."""

    def __init__(self, api_key: str | SecretStr | None = None, base_url: str = "https://api.openai.com/v1") -> None:
        self._api_key = api_key or get_key("openai")
        if hasattr(self._api_key, 'get_secret_value'):
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
            raise ProviderAuthenticationError("OpenAI API key not found in configuration or environment")
        if not self.client:
            headers = {"Authorization": f"Bearer {self._api_key}"}
            self.client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        """Return the current OpenAI model lineup."""
        await self.initialize()
        return [
            ModelDescriptor(
                model_id="gpt-4o",
                display_name="GPT-4o",
                provider_id="openai",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.EXPERT,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.EXPERT,
                    "summarization": CapabilityLevel.EXPERT,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.EXPERT,
                },
                is_local=False,
            ),
            ModelDescriptor(
                model_id="gpt-4o-mini",
                display_name="GPT-4o Mini",
                provider_id="openai",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
            ),
            ModelDescriptor(
                model_id="gpt-3.5-turbo",
                display_name="GPT-3.5 Turbo",
                provider_id="openai",
                context_length=16385,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.INTERMEDIATE,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.INTERMEDIATE,
                    "long_context": CapabilityLevel.BASIC,
                },
                is_local=False,
                tags=["fallback"],
            ),
        ]

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Standard chat inference."""
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

            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            usage = data.get("usage", {})
            return InferenceResponse(
                content=data["choices"][0]["message"]["content"],
                model_id=request.model_id,
                finish_reason=data["choices"][0]["finish_reason"] or "stop",
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=latency,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"OpenAI completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Streaming chat completions."""
        await self.initialize()
        assert self.client is not None
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
                            yield StreamChunk(
                                content=delta.get("content", ""),
                                finish_reason=data["choices"][0].get("finish_reason"),
                            )
                        except (json.JSONDecodeError, KeyError):
                            continue
        except httpx.HTTPError as e:
            raise InferenceError(f"OpenAI stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Generate batch embeddings."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.post("/embeddings", json={"model": model_id, "input": texts})
            response.raise_for_status()
            data = response.json()
            # Sort by index to maintain token alignments
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in sorted_data]
        except httpx.HTTPError as e:
            raise InferenceError(f"OpenAI embedding failed: {e}")

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
