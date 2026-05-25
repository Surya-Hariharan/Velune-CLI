"""Provider abstraction base interfaces and capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth


@runtime_checkable
class ModelProvider(Protocol):
    """Core provider contract. All LLM and Embedding adapters implement this."""

    @property
    def provider_id(self) -> str:
        """The distinct ID slug of this provider (e.g., 'ollama', 'openai')."""
        ...

    async def list_models(self) -> list[ModelDescriptor]:
        """Query and list all active/available models for this provider."""
        ...

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Single-turn, non-streaming model completion."""
        ...

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Multi-turn, token-streaming model completion."""
        ...

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Generate vector embeddings. Raises NotImplementedError if unsupported."""
        ...

    async def health_check(self) -> ProviderHealth:
        """Query host or ping API to verify provider state."""
        ...

    def get_capabilities(self) -> ProviderCapabilities:
        """Query static/dynamic capabilities of the provider."""
        ...

    async def initialize(self) -> None:
        """Perform provider connection setup and warmups."""
        ...

    async def shutdown(self) -> None:
        """Gracefully release provider connection resource pools."""
        ...


class InferenceEngine(ABC):
    """Abstract inference engine for unified task executions."""

    @abstractmethod
    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Perform non-streaming inference."""
        pass

    @abstractmethod
    async def infer_stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Perform streaming inference."""
        pass


class EmbeddingProvider(ABC):
    """Abstract embedding provider interface."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        pass

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        pass

    @abstractmethod
    def get_dimension(self) -> int:
        """Get the embedding dimension vector width."""
        pass
