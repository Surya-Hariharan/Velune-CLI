"""OpenAI provider adapter implementation."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator

import httpx

from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
    ProviderConnectionError,
)
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider


class OpenAIProvider(ModelProvider):
    """OpenAI provider for GPT chat and embedding models."""

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.openai.com/v1") -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
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
        """Fetch list of models and filter chat options."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.get("/models")
            response.raise_for_status()
            data = response.json()

            descriptors: list[ModelDescriptor] = []
            for item in data.get("data", []):
                m_id = item["id"]
                if "gpt" in m_id.lower() or "o1" in m_id.lower():
                    context_len = 128000 if "gpt-4" in m_id or "o1" in m_id else 16385
                    descriptors.append(
                        ModelDescriptor(
                            id=m_id,
                            name=m_id,
                            provider="openai",
                            context_window=context_len,
                            capabilities={
                                "coding": CapabilityLevel.ADVANCED,
                                "reasoning": CapabilityLevel.EXPERT,
                                "planning": CapabilityLevel.EXPERT,
                                "summarization": CapabilityLevel.ADVANCED,
                                "embedding": CapabilityLevel.INTERMEDIATE,
                                "instruction_following": CapabilityLevel.EXPERT,
                                "multimodal": CapabilityLevel.ADVANCED,
                                "tool_use": CapabilityLevel.EXPERT,
                                "long_context": CapabilityLevel.STRONG,
                            },
                            is_local=False,
                        )
                    )
            return descriptors
        except httpx.HTTPError as e:
            raise ProviderConnectionError(f"OpenAI connection error: {e}")

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

            return InferenceResponse(
                content=data["choices"][0]["message"]["content"],
                model_id=request.model_id,
                finish_reason=data["choices"][0]["finish_reason"] or "stop",
                tokens_used=data["usage"]["total_tokens"],
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
            return ProviderHealth.UNHEALTHY

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
