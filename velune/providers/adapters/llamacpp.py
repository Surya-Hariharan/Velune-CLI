"""Llama.cpp local GGUF model provider adapter implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from velune.core.errors.provider import InferenceError, ProviderConnectionError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider


class LlamaCppProvider(ModelProvider):
    """Llama.cpp provider for running in-process GGUF models."""

    def __init__(self, models_dir: str | None = None) -> None:
        self._models_dir = Path(models_dir) if models_dir else Path.home() / "models"
        self._loaded_models = {}
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=False,
            supports_embeddings=True,
            max_context_window=32768,
        )

    @property
    def provider_id(self) -> str:
        return "llamacpp"

    async def initialize(self) -> None:
        """Verify llama-cpp-python library is available."""
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            raise ProviderConnectionError(
                "llama-cpp-python dependency is missing. Install with: pip install llama-cpp-python"
            )

    def _resolve_model_path(self, model_id: str) -> Path:
        """Resolve the GGUF model path from model ID."""
        from velune.providers.local_paths import get_model_path, save_model_path
        from velune.providers.local_resolver import LocalModelResolver

        # 1. Check persistent cache first
        cached = get_model_path(model_id)
        if cached:
            return cached

        # 2. Ask LocalModelResolver (absolute, relative, stem scan)
        resolver = LocalModelResolver()
        found = resolver.resolve_model_path(model_id)
        if found:
            save_model_path(model_id, found)
            return found

        # 3. Interactive prompt — only in a real terminal
        prompted = resolver.prompt_for_path(model_id)
        if prompted:
            save_model_path(model_id, prompted)
            return prompted

        raise FileNotFoundError(f"GGUF model file not found for ID: {model_id}")

    def _get_model(self, model_id: str, context_window: int = 4096) -> Any:
        """Synchronously get or load the llama_cpp Llama instance."""
        if model_id in self._loaded_models:
            return self._loaded_models[model_id]

        from llama_cpp import Llama
        model_path = self._resolve_model_path(model_id)

        # Load the model in-memory.
        # Using typical defaults, letting it use GPU if compiled with CUDA/metal.
        llm = Llama(
            model_path=str(model_path),
            n_ctx=context_window,
            n_gpu_layers=-1, # Load as many layers as possible to GPU if available
            verbose=False,
        )
        self._loaded_models[model_id] = llm
        return llm

    async def list_models(self) -> list[ModelDescriptor]:
        """List local GGUF models via filesystem discovery."""
        await self.initialize()
        from velune.providers.discovery.gguf import GGUFDiscovery
        return await GGUFDiscovery().discover()

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Non-blocking in-process inference using asyncio thread offloading."""
        await self.initialize()
        start = time.perf_counter()

        try:
            # Resolve model context window size
            ctx_len = request.max_tokens or 4096
            llm = await asyncio.to_thread(self._get_model, request.model_id, ctx_len)

            # Map standard messages to llama_cpp chat completions format
            messages = [{"role": msg.get("role"), "content": msg.get("content")} for msg in request.messages]

            completion = await asyncio.to_thread(
                llm.create_chat_completion,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stream=False,
            )

            latency = (time.perf_counter() - start) * 1000.0
            choice = completion["choices"][0]

            return InferenceResponse(
                content=choice["message"]["content"] or "",
                model_id=request.model_id,
                finish_reason=choice.get("finish_reason") or "stop",
                tokens_used=completion.get("usage", {}).get("total_tokens", 0),
                latency_ms=latency,
            )
        except Exception as e:
            raise InferenceError(f"Local llama.cpp inference failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Streaming chat completions in non-blocking fashion."""
        await self.initialize()

        try:
            ctx_len = request.max_tokens or 4096
            llm = await asyncio.to_thread(self._get_model, request.model_id, ctx_len)
            messages = [{"role": msg.get("role"), "content": msg.get("content")} for msg in request.messages]

            # Run the generator in a thread pool and yield chunks back to async loop
            def run_stream():
                return llm.create_chat_completion(
                    messages=messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    top_p=request.top_p,
                    stream=True,
                )

            stream_gen = await asyncio.to_thread(run_stream)

            # Helper to fetch next item synchronously in thread
            def next_chunk(iterator):
                try:
                    return next(iterator)
                except StopIteration:
                    return None

            while True:
                chunk = await asyncio.to_thread(next_chunk, stream_gen)
                if chunk is None:
                    break

                choice = chunk["choices"][0]
                delta = choice.get("delta", {})
                content = delta.get("content", "")
                finish = choice.get("finish_reason")

                yield StreamChunk(
                    content=content,
                    finish_reason=finish,
                )

        except Exception as e:
            raise InferenceError(f"Local llama.cpp streaming failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Generate batch embeddings in-process."""
        await self.initialize()

        try:
            llm = await asyncio.to_thread(self._get_model, model_id)
            embeddings = []
            for text in texts:
                res = await asyncio.to_thread(llm.create_embedding, input=text)
                embeddings.append(res["data"][0]["embedding"])
            return embeddings
        except Exception as e:
            raise InferenceError(f"Local llama.cpp embedding failed: {e}")

    async def health_check(self) -> ProviderHealth:
        """Pings provider availability."""
        try:
            await self.initialize()
            return ProviderHealth.HEALTHY
        except Exception:
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        """Release loaded model states."""
        self._loaded_models.clear()
