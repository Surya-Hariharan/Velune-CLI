"""Ollama provider adapter implementation."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

import httpx

from velune.core.errors.provider import InferenceError, ProviderConnectionError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.providers.adapters.ollama")


class OllamaProvider(ModelProvider):
    """Ollama provider for local models."""

    def __init__(self, base_url: str | None = None) -> None:
        # Honour OLLAMA_HOST when no explicit URL is configured, so a daemon on
        # a non-default host/port (or a relocated setup) works out of the box.
        if base_url is None:
            import os

            host = os.environ.get("OLLAMA_HOST", "").strip()
            if not host:
                base_url = "http://localhost:11434"
            elif host.startswith(("http://", "https://")):
                base_url = host.rstrip("/")
            else:
                base_url = f"http://{host}"
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
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

    async def authenticate(self) -> None:
        """Ollama is local and unauthenticated."""
        pass

    async def reconnect(self) -> None:
        """Attempt to re-establish connection."""
        await self.shutdown()
        await self.initialize()

    async def _get_model_context_length(self, model_name: str) -> int:
        """Query /api/show for the model's actual context window size.

        Ollama's ``/api/show`` returns a ``parameters`` string that may contain
        a line like ``num_ctx                        131072``.  We parse that to
        get the real context length instead of hard-coding 8192.

        Falls back to 8 192 if the endpoint is unreachable, the model is not
        loaded, or the field is absent.
        """
        assert self.client is not None
        try:
            resp = await self.client.post("/api/show", json={"name": model_name})
            if resp.status_code != 200:
                return 8192
            data = resp.json()
            # ``parameters`` is a newline-delimited string of key-value pairs
            params_str: str = data.get("parameters", "")
            for line in params_str.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "num_ctx":
                    try:
                        return int(parts[1])
                    except ValueError:
                        pass
            # Fallback: check model_info dict returned by newer Ollama builds
            model_info: dict = data.get("model_info", {})
            for key, value in model_info.items():
                if "context" in key.lower() and isinstance(value, int) and value > 0:
                    return value
        except Exception as exc:
            logger.debug("Could not fetch context length for %s: %s", model_name, exc)
        return 8192

    async def list_models(self) -> list[ModelDescriptor]:
        """Fetch models from active Ollama endpoint with accurate context lengths.

        Queries ``/api/show`` for each model to populate the real ``num_ctx``
        value instead of defaulting every model to 8 192 tokens.
        """
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()

            descriptors: list[ModelDescriptor] = []
            for item in data.get("models", []):
                model_name = item["name"]
                ctx_len = await self._get_model_context_length(model_name)
                descriptors.append(
                    ModelDescriptor(
                        model_id=model_name,
                        display_name=model_name,
                        provider_id="ollama",
                        context_length=ctx_len,
                        capabilities={
                            "coding": CapabilityLevel.INTERMEDIATE,
                            "reasoning": CapabilityLevel.INTERMEDIATE,
                            "planning": CapabilityLevel.BASIC,
                            "summarization": CapabilityLevel.INTERMEDIATE,
                            "embedding": CapabilityLevel.INTERMEDIATE,
                            "instruction_following": CapabilityLevel.INTERMEDIATE,
                            "multimodal": CapabilityLevel.NONE,
                            "tool_use": CapabilityLevel.NONE,
                            "long_context": (
                                CapabilityLevel.INTERMEDIATE
                                if ctx_len > 32768
                                else CapabilityLevel.NONE
                            ),
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
                    request.model_id,
                    latency / 1000.0,
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

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Batch embedding generation."""
        await self.initialize()
        assert self.client is not None
        embeddings: list[list[float]] = []
        try:
            for text in texts:
                resp = await self.client.post(
                    "/api/embeddings", json={"model": model_id, "prompt": text}
                )
                resp.raise_for_status()
                embeddings.append(resp.json()["embedding"])
            return embeddings
        except httpx.HTTPError as e:
            raise InferenceError(f"Ollama embedding failed: {e}")

    async def health_check(self) -> ProviderHealth:
        """Pings Ollama core endpoint and checks process if unreachable."""
        await self.initialize()
        assert self.client is not None
        try:
            resp = await self.client.get("/")
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            return ProviderHealth.DEGRADED
        except Exception:
            # Fallback to checking if the process is running locally
            if "localhost" in self._base_url or "127.0.0.1" in self._base_url:
                import psutil
                try:
                    for proc in psutil.process_iter(['name']):
                        if proc.info['name'] and 'ollama' in proc.info['name'].lower():
                            logger.error("Ollama service is stopped. Please run `ollama serve`.")
                            return ProviderHealth.OFFLINE
                except Exception:
                    pass
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        """Close connection pools."""
        if self.client:
            await self.client.aclose()
            self.client = None
