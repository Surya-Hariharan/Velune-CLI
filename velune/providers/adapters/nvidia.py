"""NVIDIA NIM provider adapter — OpenAI-compatible API at api.nvidia.com."""

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
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key


class NVIDIAProvider(ModelProvider):
    """NVIDIA NIM provider — cloud-hosted inference via api.nvidia.com."""

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        base_url: str = "https://integrate.api.nvidia.com/v1",
    ) -> None:
        self._api_key = api_key or get_key("nvidia")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=False,
            supports_embeddings=False,
            max_context_window=128000,
        )

    @property
    def provider_id(self) -> str:
        return "nvidia"

    async def initialize(self) -> None:
        if not self._api_key:
            raise ProviderAuthenticationError(
                "NVIDIA API key not found. Set NVIDIA_API_KEY or run `velune provider add nvidia`."
            )
        if not self.client:
            self.client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=300.0,
            )

    async def list_models(self) -> list[ModelDescriptor]:
        await self.initialize()
        return [
            ModelDescriptor(
                model_id="meta/llama-3.3-70b-instruct",
                display_name="Llama 3.3 70B Instruct (NIM)",
                provider_id="nvidia",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.ADVANCED,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                cost_per_1k_tokens=0.00027,
            ),
            ModelDescriptor(
                model_id="mistralai/mistral-large-2-instruct",
                display_name="Mistral Large 2 (NIM)",
                provider_id="nvidia",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.EXPERT,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                cost_per_1k_tokens=0.002,
            ),
            ModelDescriptor(
                model_id="nvidia/llama-3.1-nemotron-70b-instruct",
                display_name="Nemotron 70B Instruct",
                provider_id="nvidia",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.EXPERT,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.ADVANCED,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                cost_per_1k_tokens=0.00035,
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
                finish_reason=data["choices"][0].get("finish_reason") or "stop",
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=latency,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ProviderAuthenticationError("NVIDIA API key is invalid or expired.")
            raise InferenceError(f"NVIDIA NIM completion failed: {e}")
        except httpx.HTTPError as e:
            raise InferenceError(f"NVIDIA NIM completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
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
            raise InferenceError(f"NVIDIA NIM stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        raise NotImplementedError("Use NVIDIA NIM embedding-specific endpoints directly.")

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
