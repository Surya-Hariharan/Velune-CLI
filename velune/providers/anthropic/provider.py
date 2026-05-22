"""Anthropic provider implementation."""

import httpx
import os
from typing import AsyncIterator, Optional
from velune.providers.base import ModelProvider
from velune.core.types import (
    InferenceRequest,
    InferenceResponse,
    StreamChunk,
    ModelDescriptor,
    ProviderCapabilities,
    ModelCapability,
    CapabilityLevel,
)
from velune.core.errors import (
    ProviderConnectionError,
    ProviderAuthenticationError,
    InferenceError,
)


class AnthropicProvider(ModelProvider):
    """Anthropic provider for Claude models."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.anthropic.com",
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=False,
            max_context_window=200000,
            rate_limit_rpm=1000,
            rate_limit_tpm=80000,
        )

    async def initialize(self) -> None:
        """Initialize the provider."""
        if not self.api_key:
            raise ProviderAuthenticationError("Anthropic API key not provided")

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        self.client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=300.0
        )

    async def list_models(self) -> list[ModelDescriptor]:
        """List available models from Anthropic."""
        # Anthropic has a fixed set of models
        models = [
            ModelDescriptor(
                id="claude-3-opus-20240229",
                name="Claude 3 Opus",
                provider="anthropic",
                context_window=200000,
                capabilities={
                    ModelCapability.CODE_GENERATION: CapabilityLevel.ADVANCED,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.ADVANCED,
                    ModelCapability.REASONING: CapabilityLevel.EXPERT,
                    ModelCapability.PLANNING: CapabilityLevel.EXPERT,
                    ModelCapability.TOOL_USE: CapabilityLevel.EXPERT,
                },
                is_local=False,
            ),
            ModelDescriptor(
                id="claude-3-sonnet-20240229",
                name="Claude 3 Sonnet",
                provider="anthropic",
                context_window=200000,
                capabilities={
                    ModelCapability.CODE_GENERATION: CapabilityLevel.ADVANCED,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.ADVANCED,
                    ModelCapability.REASONING: CapabilityLevel.ADVANCED,
                    ModelCapability.PLANNING: CapabilityLevel.ADVANCED,
                    ModelCapability.TOOL_USE: CapabilityLevel.ADVANCED,
                },
                is_local=False,
            ),
            ModelDescriptor(
                id="claude-3-haiku-20240307",
                name="Claude 3 Haiku",
                provider="anthropic",
                context_window=200000,
                capabilities={
                    ModelCapability.CODE_GENERATION: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.REASONING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.PLANNING: CapabilityLevel.INTERMEDIATE,
                },
                is_local=False,
            ),
        ]
        return models

    async def get_model(self, model_id: str) -> Optional[ModelDescriptor]:
        """Get a specific model descriptor."""
        models = await self.list_models()
        for model in models:
            if model.id == model_id:
                return model
        return None

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Perform inference."""
        if not self.client:
            await self.initialize()

        import time
        start_time = time.time()

        try:
            # Convert OpenAI-style messages to Anthropic format
            messages = []
            system_message = None
            for msg in request.messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    messages.append({"role": msg["role"], "content": msg["content"]})

            payload = {
                "model": request.model_id,
                "messages": messages,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
            }

            if system_message:
                payload["system"] = system_message

            if request.stop_sequences:
                payload["stop_sequences"] = request.stop_sequences

            response = await self.client.post("/v1/messages", json=payload)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.time() - start_time) * 1000

            return InferenceResponse(
                content=data["content"][0]["text"],
                model_id=request.model_id,
                finish_reason=data["stop_reason"],
                tokens_used=data["usage"]["input_tokens"] + data["usage"]["output_tokens"],
                latency_ms=latency_ms,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Anthropic inference failed: {e}")

    async def infer_stream(
        self, request: InferenceRequest
    ) -> AsyncIterator[StreamChunk]:
        """Perform streaming inference."""
        if not self.client:
            await self.initialize()

        try:
            # Convert OpenAI-style messages to Anthropic format
            messages = []
            system_message = None
            for msg in request.messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    messages.append({"role": msg["role"], "content": msg["content"]})

            payload = {
                "model": request.model_id,
                "messages": messages,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "stream": True,
            }

            if system_message:
                payload["system"] = system_message

            if request.stop_sequences:
                payload["stop_sequences"] = request.stop_sequences

            async with self.client.stream(
                "POST", "/v1/messages", json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "event_type: message_stop":
                            break
                        import json
                        try:
                            data = json.loads(data_str)
                            if data.get("type") == "content_block_delta":
                                yield StreamChunk(
                                    content=data["delta"]["text"],
                                    finish_reason=None,
                                )
                            elif data.get("type") == "message_stop":
                                yield StreamChunk(
                                    content="",
                                    finish_reason=data["stop_reason"],
                                )
                        except (json.JSONDecodeError, KeyError):
                            continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Anthropic streaming inference failed: {e}")

    def get_capabilities(self) -> ProviderCapabilities:
        """Get provider capabilities."""
        return self._capabilities

    async def health_check(self) -> bool:
        """Check if Anthropic is healthy."""
        if not self.client:
            await self.initialize()

        try:
            response = await self.client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-haiku-20240307",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 10,
                },
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def shutdown(self) -> None:
        """Shutdown the provider."""
        if self.client:
            await self.client.aclose()
            self.client = None
