"""Anthropic provider adapter implementation."""

from __future__ import annotations

import httpx
import json
import os
import time
from typing import AsyncIterator, List, Optional
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelCapability, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.core.errors.provider import ProviderConnectionError, ProviderAuthenticationError, InferenceError


class AnthropicProvider(ModelProvider):
    """Anthropic provider for Claude models."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.anthropic.com") -> None:
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=False,
            max_context_window=200000,
        )

    @property
    def provider_id(self) -> str:
        return "anthropic"

    async def initialize(self) -> None:
        """Initialize HTTP client with Anthropic specific headers."""
        if not self._api_key:
            raise ProviderAuthenticationError("Anthropic API key not found in configuration or environment")
        if not self.client:
            headers = {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            self.client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=300.0)

    async def list_models(self) -> List[ModelDescriptor]:
        """List active Claude models."""
        await self.initialize()
        # Anthropic has static lists, or we can query their endpoints. Here we provide the standard suite.
        return [
            ModelDescriptor(
                id="claude-3-5-sonnet-20241022",
                name="Claude 3.5 Sonnet",
                provider="anthropic",
                context_window=200000,
                capabilities={
                    "coding": CapabilityLevel.EXPERT,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.EXPERT,
                    "summarization": CapabilityLevel.EXPERT,
                    "embedding": CapabilityLevel.NONE,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "multimodal": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.EXCEPTIONAL,
                },
                is_local=False,
            ),
            ModelDescriptor(
                id="claude-3-5-haiku-20241022",
                name="Claude 3.5 Haiku",
                provider="anthropic",
                context_window=200000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "embedding": CapabilityLevel.NONE,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "multimodal": CapabilityLevel.NONE,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.EXCEPTIONAL,
                },
                is_local=False,
            ),
        ]

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Perform Claude inference."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            # Map standard messages to Anthropic format (system role is parsed out if present)
            system_prompt = ""
            anth_messages = []
            for msg in request.messages:
                if msg.get("role") == "system":
                    system_prompt = msg.get("content", "")
                else:
                    anth_messages.append({"role": msg.get("role"), "content": msg.get("content")})

            payload = {
                "model": request.model_id,
                "messages": anth_messages,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
            }
            if system_prompt:
                payload["system"] = system_prompt
            if request.stop_sequences:
                payload["stop_sequences"] = request.stop_sequences

            response = await self.client.post("/v1/messages", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            content = ""
            if data.get("content"):
                content = data["content"][0].get("text", "")

            return InferenceResponse(
                content=content,
                model_id=request.model_id,
                finish_reason=data.get("stop_reason") or "end_turn",
                tokens_used=data.get("usage", {}).get("total_tokens", 0),
                latency_ms=latency,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Anthropic message completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Perform streaming completion."""
        await self.initialize()
        assert self.client is not None
        try:
            system_prompt = ""
            anth_messages = []
            for msg in request.messages:
                if msg.get("role") == "system":
                    system_prompt = msg.get("content", "")
                else:
                    anth_messages.append({"role": msg.get("role"), "content": msg.get("content")})

            payload = {
                "model": request.model_id,
                "messages": anth_messages,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "stream": True,
            }
            if system_prompt:
                payload["system"] = system_prompt
            if request.stop_sequences:
                payload["stop_sequences"] = request.stop_sequences

            async with self.client.stream("POST", "/v1/messages", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        try:
                            data = json.loads(data_str)
                            d_type = data.get("type")
                            if d_type == "content_block_delta":
                                yield StreamChunk(
                                    content=data["delta"].get("text", ""),
                                )
                            elif d_type == "message_delta":
                                yield StreamChunk(
                                    content="",
                                    finish_reason=data.get("delta", {}).get("stop_reason"),
                                )
                        except (json.JSONDecodeError, KeyError):
                            continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Anthropic stream failed: {e}")

    async def embed(self, texts: List[str], model_id: str) -> List[List[float]]:
        raise NotImplementedError("Anthropic provider does not support embeddings.")

    async def health_check(self) -> ProviderHealth:
        """Simple validation verification."""
        try:
            await self.initialize()
            assert self.client is not None
            # Fetch a simple request with 1 token output
            payload = {
                "model": "claude-3-5-haiku-20241022",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            resp = await self.client.post("/v1/messages", json=payload)
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
