"""Anthropic provider adapter implementation."""

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


class AnthropicProvider(ModelProvider):
    """Anthropic provider for Claude models."""

    def __init__(
        self, api_key: str | SecretStr | None = None, base_url: str = "https://api.anthropic.com"
    ) -> None:
        self._api_key = api_key or get_key("anthropic")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
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
            raise ProviderAuthenticationError(
                "Anthropic API key not found in configuration or environment"
            )
        if not self.client:
            headers = {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                # Enable prompt-caching beta. Harmless when no cache_control blocks
                # are present in the payload — activates automatically when they are.
                "anthropic-beta": "prompt-caching-2024-07-31",
                "content-type": "application/json",
            }
            self.client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        """List active Claude models."""
        await self.initialize()
        # Anthropic has static lists, or we can query their endpoints. Here we provide the standard suite.
        return [
            ModelDescriptor(
                model_id="claude-opus-4-5",
                display_name="Claude Opus 4.5",
                provider_id="anthropic",
                context_length=200000,
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
                model_id="claude-sonnet-4-5",
                display_name="Claude Sonnet 4.5",
                provider_id="anthropic",
                context_length=200000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
            ),
            ModelDescriptor(
                model_id="claude-haiku-4-5",
                display_name="Claude Haiku 4.5",
                provider_id="anthropic",
                context_length=200000,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.INTERMEDIATE,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.INTERMEDIATE,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.ADVANCED,
                    "long_context": CapabilityLevel.INTERMEDIATE,
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
            payload = self._build_payload(request)

            response = await self.client.post("/v1/messages", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            content = ""
            if data.get("content"):
                content = data["content"][0].get("text", "")

            # Record latency in health monitor if available
            self._record_latency_to_monitor(latency)

            usage = data.get("usage", {})
            return InferenceResponse(
                content=content,
                model_id=request.model_id,
                finish_reason=data.get("stop_reason") or "end_turn",
                tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                latency_ms=latency,
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                metadata={"raw_usage": usage},
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Anthropic message completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Perform streaming completion."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        first_token = True
        try:
            payload = self._build_payload(request)
            payload["stream"] = True

            async with self.client.stream("POST", "/v1/messages", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        try:
                            data = json.loads(data_str)
                            d_type = data.get("type")
                            if d_type == "content_block_delta":
                                # Record latency at first token
                                if first_token:
                                    latency = (time.perf_counter() - start) * 1000.0
                                    self._record_latency_to_monitor(latency)
                                    first_token = False
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

    def _build_payload(self, request: InferenceRequest) -> dict:
        """Build the Anthropic API payload for *request*.

        When the cache manager has pre-transformed the messages into
        cache_control block format (stored in metadata), use that directly.
        Otherwise fall back to plain string extraction.
        """
        from velune.context.cache.providers import ANTHROPIC_CACHE_PAYLOAD_KEY

        cache_payload = request.metadata.get(ANTHROPIC_CACHE_PAYLOAD_KEY)
        if cache_payload:
            payload: dict = {
                "model": request.model_id,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "messages": cache_payload["messages"],
            }
            if "system" in cache_payload:
                payload["system"] = cache_payload["system"]
        else:
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
        return payload

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        raise NotImplementedError("Anthropic provider does not support embeddings.")

    def _record_latency_to_monitor(self, latency_ms: float) -> None:
        """Record latency to health monitor if available."""
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if container.has("runtime.provider_health_monitor"):
                monitor = container.get("runtime.provider_health_monitor")
                monitor.record_latency(self.provider_id, int(latency_ms))
        except (ImportError, AttributeError, KeyError):
            pass  # Health monitor not available, skip

    async def health_check(self) -> ProviderHealth:
        """Simple validation verification."""
        try:
            await self.initialize()
            assert self.client is not None
            # Fetch a simple request with 1 token output
            payload = {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            resp = await self.client.post("/v1/messages", json=payload)
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
