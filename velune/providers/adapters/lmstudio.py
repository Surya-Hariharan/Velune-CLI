"""LM Studio provider adapter implementation."""

from __future__ import annotations

import httpx
import json
import time
from typing import AsyncIterator, List, Optional
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelCapability, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.core.errors.provider import ProviderConnectionError, InferenceError


class LMStudioProvider(ModelProvider):
    """LM Studio provider for local OpenAI-compatible endpoints."""

    def __init__(self, base_url: str = "http://localhost:1234/v1") -> None:
        self._base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None
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

    async def list_models(self) -> List[ModelDescriptor]:
        """Fetch list of active models loaded in LM Studio."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.get("/models")
            response.raise_for_status()
            data = response.json()
            
            descriptors: List[ModelDescriptor] = []
            for item in data.get("data", []):
                m_id = item["id"]
                descriptors.append(
                    ModelDescriptor(
                        id=m_id,
                        name=m_id,
                        provider="lmstudio",
                        context_window=32768,
                        capabilities={
                            "coding": CapabilityLevel.INTERMEDIATE,
                            "reasoning": CapabilityLevel.INTERMEDIATE,
                            "planning": CapabilityLevel.BASIC,
                            "summarization": CapabilityLevel.INTERMEDIATE,
                            "embedding": CapabilityLevel.INTERMEDIATE,
                            "instruction_following": CapabilityLevel.CAPABLE,
                            "multimodal": CapabilityLevel.NONE,
                            "tool_use": CapabilityLevel.CAPABLE,
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
                tokens_used=data.get("usage", {}).get("total_tokens", 0),
                latency_ms=latency,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"LM Studio completion failed: {e}")

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
            raise InferenceError(f"LM Studio stream failed: {e}")

    async def embed(self, texts: List[str], model_id: str) -> List[List[float]]:
        """Generate batch embeddings."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.post("/embeddings", json={"model": model_id, "input": texts})
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
            return ProviderHealth.UNHEALTHY

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
