"""Ollama provider implementation."""

import httpx
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


class OllamaProvider(ModelProvider):
    """Ollama provider for local models."""

    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url
        self.client: Optional[httpx.AsyncClient] = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=False,
            supports_embeddings=True,
            max_context_window=None,  # Varies by model
            rate_limit_rpm=None,
            rate_limit_tpm=None,
        )

    async def initialize(self) -> None:
        """Initialize the provider."""
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        """List available models from Ollama."""
        if not self.client:
            await self.initialize()

        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
            
            models = []
            for model in data.get("models", []):
                models.append(
                    ModelDescriptor(
                        id=model["name"],
                        name=model["name"],
                        provider="ollama",
                        context_window=4096,  # Default, will be updated per model
                        capabilities={
                            ModelCapability.CODE_GENERATION: CapabilityLevel.INTERMEDIATE,
                            ModelCapability.CODE_ANALYSIS: CapabilityLevel.INTERMEDIATE,
                            ModelCapability.REASONING: CapabilityLevel.INTERMEDIATE,
                        },
                        is_local=True,
                    )
                )
            return models
        except httpx.HTTPError as e:
            raise ProviderConnectionError(f"Failed to connect to Ollama: {e}")

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

            latency_ms = (time.time() - start_time) * 1000

            return InferenceResponse(
                content=data["message"]["content"],
                model_id=request.model_id,
                finish_reason=data.get("done_reason", "stop"),
                tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
                latency_ms=latency_ms,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Ollama inference failed: {e}")

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
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_tokens,
                    "top_p": request.top_p,
                },
            }

            if request.stop_sequences:
                payload["options"]["stop"] = request.stop_sequences

            async with self.client.stream(
                "POST", "/api/chat", json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        import json
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
            raise InferenceError(f"Ollama streaming inference failed: {e}")

    def get_capabilities(self) -> ProviderCapabilities:
        """Get provider capabilities."""
        return self._capabilities

    async def health_check(self) -> bool:
        """Check if Ollama is healthy."""
        if not self.client:
            await self.initialize()

        try:
            response = await self.client.get("/")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def shutdown(self) -> None:
        """Shutdown the provider."""
        if self.client:
            await self.client.aclose()
            self.client = None
