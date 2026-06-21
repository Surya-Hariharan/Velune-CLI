"""Cohere provider adapter."""

from __future__ import annotations

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


def _to_cohere_messages(messages: list[dict]) -> tuple[str, list[dict], str]:
    """Convert OpenAI-style messages to Cohere chat history + preamble."""
    preamble = ""
    history: list[dict] = []
    last_user_msg = ""

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            preamble = content
        elif role == "user":
            if last_user_msg:
                history.append({"role": "USER", "message": last_user_msg})
            last_user_msg = content
        elif role == "assistant":
            history.append({"role": "CHATBOT", "message": content})

    return preamble, history, last_user_msg


class CohereProvider(ModelProvider):
    """Cohere provider — Command R and embed models."""

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        base_url: str = "https://api.cohere.com/v1",
    ) -> None:
        self._api_key = api_key or get_key("cohere")
        if hasattr(self._api_key, "get_secret_value"):
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
        return "cohere"

    async def initialize(self) -> None:
        if not self._api_key:
            raise ProviderAuthenticationError(
                "Cohere API key not found. Set COHERE_API_KEY or run `velune provider add cohere`."
            )
        if not self.client:
            self.client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Name": "velune",
                },
                timeout=300.0,
            )

    async def list_models(self) -> list[ModelDescriptor]:
        await self.initialize()
        return [
            ModelDescriptor(
                model_id="command-r-plus-08-2024",
                display_name="Command R+ (Aug 2024)",
                provider_id="cohere",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.EXPERT,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.EXPERT,
                },
                is_local=False,
                cost_per_1k_tokens=0.00265,
            ),
            ModelDescriptor(
                model_id="command-r-08-2024",
                display_name="Command R (Aug 2024)",
                provider_id="cohere",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.ADVANCED,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                cost_per_1k_tokens=0.000375,
            ),
        ]

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            preamble, history, message = _to_cohere_messages(request.messages)
            payload: dict = {
                "model": request.model_id,
                "message": message,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            }
            if preamble:
                payload["preamble"] = preamble
            if history:
                payload["chat_history"] = history

            response = await self.client.post("/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            meta = data.get("meta", {})
            tokens = meta.get("tokens", {})
            input_tokens = tokens.get("input_tokens", 0)
            output_tokens = tokens.get("output_tokens", 0)

            return InferenceResponse(
                content=data.get("text", ""),
                model_id=request.model_id,
                finish_reason=data.get("finish_reason", "COMPLETE").lower(),
                tokens_used=input_tokens + output_tokens,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                latency_ms=latency,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ProviderAuthenticationError("Cohere API key is invalid or expired.")
            raise InferenceError(f"Cohere chat failed: {e}")
        except httpx.HTTPError as e:
            raise InferenceError(f"Cohere chat failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        await self.initialize()
        assert self.client is not None
        try:
            import json

            preamble, history, message = _to_cohere_messages(request.messages)
            payload: dict = {
                "model": request.model_id,
                "message": message,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "stream": True,
            }
            if preamble:
                payload["preamble"] = preamble
            if history:
                payload["chat_history"] = history

            async with self.client.stream("POST", "/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        event_type = data.get("event_type", "")
                        if event_type == "text-generation":
                            yield StreamChunk(content=data.get("text", ""))
                        elif event_type == "stream-end":
                            yield StreamChunk(
                                content="",
                                finish_reason=data.get("finish_reason", "COMPLETE").lower(),
                            )
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Cohere stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.post(
                "/embed",
                json={
                    "model": model_id or "embed-english-v3.0",
                    "texts": texts,
                    "input_type": "search_document",
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["embeddings"]
        except httpx.HTTPError as e:
            raise InferenceError(f"Cohere embed failed: {e}")

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
