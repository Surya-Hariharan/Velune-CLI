"""Ollama provider adapter implementation."""

from __future__ import annotations

import httpx
import json
import time
import logging
from typing import AsyncIterator, List, Optional
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelCapability, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.core.errors.provider import ProviderConnectionError, InferenceError

logger = logging.getLogger("velune.providers.adapters.ollama")


class OllamaProvider(ModelProvider):
    """Ollama provider for local models."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=False,
            supports_embeddings=True,
            max_context_window=8192,
        )

    @property
    def provider_id(self) -> str:
        return "ollama"

    async def initialize(self) -> None:
        """Initialize the async client."""
        if not self.client:
            self.client = httpx.AsyncClient(base_url=self._base_url, timeout=300.0)

    async def list_models(self) -> List[ModelDescriptor]:
        """Fetch models from active Ollama endpoint."""
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
            
            descriptors: List[ModelDescriptor] = []
            for item in data.get("models", []):
                descriptors.append(
                    ModelDescriptor(
                        id=item["name"],
                        name=item["name"],
                        provider="ollama",
                        context_window=8192,
                        capabilities={
                            "coding": CapabilityLevel.INTERMEDIATE,
                            "reasoning": CapabilityLevel.INTERMEDIATE,
                            "planning": CapabilityLevel.BASIC,
                            "summarization": CapabilityLevel.INTERMEDIATE,
                            "embedding": CapabilityLevel.INTERMEDIATE,
                            "instruction_following": CapabilityLevel.CAPABLE,
                            "multimodal": CapabilityLevel.NONE,
                            "tool_use": CapabilityLevel.NONE,
                            "long_context": CapabilityLevel.NONE,
                        },
                        is_local=True,
                    )
                )
            return descriptors
        except httpx.HTTPError as e:
            raise ProviderConnectionError(f"Failed to fetch models from Ollama: {e}")

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Synchronous chat inference."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            payload = {
                "model": request.model_id,
                "messages": request.messages,
                "stream": False,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_tokens,
                    "top_p": request.top_p,
                },
            }
            if request.stop_sequences:
                payload["options"]["stop"] = request.stop_sequences

            response = await self.client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            if latency > 30000.0:
                logger.warning(
                    "Slow inference on %s (%.1fs). Consider a smaller model for your hardware.", 
                    request.model_id, latency / 1000.0
                )

            return InferenceResponse(
                content=data["message"]["content"],
                model_id=request.model_id,
                finish_reason=data.get("done_reason", "stop"),
                tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
                latency_ms=latency,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Ollama inference failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Streaming chat completion."""
        await self.initialize()
        assert self.client is not None
        try:
            payload = {
                "model": request.model_id,
                "messages": request.messages,
                "stream": True,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_tokens,
                    "top_p": request.top_p,
                },
            }
            if request.stop_sequences:
                payload["options"]["stop"] = request.stop_sequences

            async with self.client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if "message" in data:
                            yield StreamChunk(
                                content=data["message"].get("content", ""),
                                finish_reason=data.get("done_reason"),
                            )
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Ollama streaming failed: {e}")

    async def embed(self, texts: List[str], model_id: str) -> List[List[float]]:
        """Batch embedding generation."""
        await self.initialize()
        assert self.client is not None
        embeddings: List[List[float]] = []
        try:
            for text in texts:
                resp = await self.client.post("/api/embeddings", json={"model": model_id, "prompt": text})
                resp.raise_for_status()
                embeddings.append(resp.json()["embedding"])
            return embeddings
        except httpx.HTTPError as e:
            raise InferenceError(f"Ollama embedding failed: {e}")

    async def health_check(self) -> ProviderHealth:
        """Pings Ollama core endpoint."""
        await self.initialize()
        assert self.client is not None
        try:
            resp = await self.client.get("/")
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNHEALTHY

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        """Close connection pools."""
        if self.client:
            await self.client.aclose()
            self.client = None
