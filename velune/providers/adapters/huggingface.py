"""Hugging Face provider adapter implementation."""

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
from velune.core.types.model import ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key


class HuggingFaceProvider(ModelProvider):
    """Hugging Face provider for serverless Inference API."""

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        base_url: str = "https://api-inference.huggingface.co",
    ) -> None:
        self._api_key = api_key or get_key("huggingface")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=False,
            supports_embeddings=True,
            max_context_window=32768,
        )

    @property
    def provider_id(self) -> str:
        return "huggingface"

    async def initialize(self) -> None:
        """Initialize client headers."""
        if not self._api_key:
            raise ProviderAuthenticationError(
                "Hugging Face API token (HF_TOKEN) not found in environment or config"
            )
        if not self.client:
            headers = {"Authorization": f"Bearer {self._api_key}"}
            self.client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        """Fetch list of local cached Hugging Face models."""
        from velune.providers.discovery.huggingface import HuggingFaceDiscovery

        discovery = HuggingFaceDiscovery()
        return await discovery.discover()

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Query Hugging Face serverless chat completion API."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            # Map standard messages to conversational prompt
            prompt = self._format_messages_to_prompt(request.messages)

            payload = {
                "inputs": prompt,
                "parameters": {
                    "temperature": request.temperature,
                    "max_new_tokens": request.max_tokens or 1024,
                    "top_p": request.top_p,
                },
                "options": {"wait_for_model": True},
            }

            model_path = f"/models/{request.model_id}"
            response = await self.client.post(model_path, json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            # HF Serverless response formatting varies by model/pipeline type
            content = ""
            if isinstance(data, list) and len(data) > 0:
                content = data[0].get("generated_text", "")
                # Strip the prompt from generation if the model prepends it
                if content.startswith(prompt):
                    content = content[len(prompt) :]
            elif isinstance(data, dict):
                content = data.get("generated_text", "")

            return InferenceResponse(
                content=content.strip(),
                model_id=request.model_id,
                finish_reason="stop",
                tokens_used=0,  # HF serverless doesn't return exact token metrics consistently
                latency_ms=latency,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Hugging Face Inference completion failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Stream conversational replies from Serverless Inference API."""
        await self.initialize()
        assert self.client is not None
        try:
            prompt = self._format_messages_to_prompt(request.messages)
            payload = {
                "inputs": prompt,
                "parameters": {
                    "temperature": request.temperature,
                    "max_new_tokens": request.max_tokens or 1024,
                    "top_p": request.top_p,
                },
                "options": {"wait_for_model": True},
                "stream": True,
            }

            model_path = f"/models/{request.model_id}"
            async with self.client.stream("POST", model_path, json=payload) as response:
                response.raise_for_status()
                # Serverless stream format is line-delimited SSE chunks
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            chunk_data = json.loads(line[5:])
                            token_text = chunk_data.get("token", {}).get("text", "")
                            yield StreamChunk(
                                content=token_text,
                                finish_reason="stop"
                                if chunk_data.get("token", {}).get("special", False)
                                else None,
                            )
                        except Exception:
                            continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Hugging Face Inference streaming failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Batch embeddings generation using HF feature-extraction pipeline."""
        await self.initialize()
        assert self.client is not None
        try:
            model_path = f"/models/{model_id}"
            response = await self.client.post(
                model_path, json={"inputs": texts, "options": {"wait_for_model": True}}
            )
            response.raise_for_status()
            embeddings = response.json()

            # Embeddings could be 1D or 2D/3D depending on token poolings. Ensure we return 2D floats.
            if isinstance(embeddings, list) and len(embeddings) > 0:
                if isinstance(embeddings[0], list):
                    # Check if it has token-level embeddings or pooled
                    if isinstance(embeddings[0][0], list):
                        # Simple average pooling for token embeddings
                        pooled = []
                        for seq in embeddings:
                            avg = [sum(col) / len(seq) for col in zip(*seq, strict=False)]
                            pooled.append(avg)
                        return pooled
                    return embeddings
                # Single sequence 1D returned, wrap in list
                return [embeddings]
            raise ValueError("Invalid embedding response structure from HF Inference API")
        except httpx.HTTPError as e:
            raise InferenceError(f"Hugging Face embedding failed: {e}")

    def _format_messages_to_prompt(self, messages: list[dict]) -> str:
        """Utility to stitch general messages into standard chat-template prompt representation."""
        prompt = ""
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                prompt += f"<|system|>\n{content}</s>\n"
            elif role == "user":
                prompt += f"<|user|>\n{content}</s>\n"
            else:
                prompt += f"<|assistant|>\n{content}</s>\n"
        prompt += "<|assistant|>\n"
        return prompt

    async def health_check(self) -> ProviderHealth:
        """Query HF API viability."""
        try:
            await self.initialize()
            assert self.client is not None
            # Fetch meta details for standard model to verify connection
            resp = await self.client.get("/models/gpt2")
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
