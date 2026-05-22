"""OpenAI provider implementation."""

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


class OpenAIProvider(ModelProvider):
    """OpenAI provider for GPT models."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.openai.com/v1",
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=True,
            max_context_window=128000,
            rate_limit_rpm=3500,
            rate_limit_tpm=200000,
        )

    async def initialize(self) -> None:
        """Initialize the provider."""
        if not self.api_key:
            raise ProviderAuthenticationError("OpenAI API key not provided")

        headers = {"Authorization": f"Bearer {self.api_key}"}
        self.client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=300.0
        )

    async def list_models(self) -> list[ModelDescriptor]:
        """List available models from OpenAI."""
        if not self.client:
            await self.initialize()

        try:
            response = await self.client.get("/models")
            response.raise_for_status()
            data = response.json()

            models = []
            for model in data.get("data", []):
                # Filter for chat models
                if "gpt" in model["id"].lower():
                    context_window = 128000 if "gpt-4" in model["id"] else 16385
                    models.append(
                        ModelDescriptor(
                            id=model["id"],
                            name=model["id"],
                            provider="openai",
                            context_window=context_window,
                            capabilities={
                                ModelCapability.CODE_GENERATION: CapabilityLevel.ADVANCED,
                                ModelCapability.CODE_ANALYSIS: CapabilityLevel.ADVANCED,
                                ModelCapability.REASONING: CapabilityLevel.EXPERT,
                                ModelCapability.PLANNING: CapabilityLevel.EXPERT,
                                ModelCapability.TOOL_USE: CapabilityLevel.EXPERT,
                            },
                            is_local=False,
                        )
                    )
            return models
        except httpx.HTTPError as e:
            raise ProviderConnectionError(f"Failed to connect to OpenAI: {e}")

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

            latency_ms = (time.time() - start_time) * 1000

            return InferenceResponse(
                content=data["choices"][0]["message"]["content"],
                model_id=request.model_id,
                finish_reason=data["choices"][0]["finish_reason"],
                tokens_used=data["usage"]["total_tokens"],
                latency_ms=latency_ms,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"OpenAI inference failed: {e}")

    async def infer_stream(
        self, request: InferenceRequest
    ) -> AsyncIterator[StreamChunk]:
        """Perform streaming inference."""
        if not self.client:
            await self.initialize()

        try:
            payload = {
                "model": request.model_id,
                "messages": request.messages,
                "stream": True,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "top_p": request.top_p,
            }

            if request.stop_sequences:
                payload["stop"] = request.stop_sequences

            async with self.client.stream(
                "POST", "/chat/completions", json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        import json
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
            raise InferenceError(f"OpenAI streaming inference failed: {e}")

    def get_capabilities(self) -> ProviderCapabilities:
        """Get provider capabilities."""
        return self._capabilities

    async def health_check(self) -> bool:
        """Check if OpenAI is healthy."""
        if not self.client:
            await self.initialize()

        try:
            response = await self.client.get("/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def shutdown(self) -> None:
        """Shutdown the provider."""
        if self.client:
            await self.client.aclose()
            self.client = None
