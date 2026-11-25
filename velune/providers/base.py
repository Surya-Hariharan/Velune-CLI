"""Provider abstraction base interfaces and capabilities."""

from __future__ import annotations

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

    async def authenticate(self) -> None:
        """Verify API keys or tokens with the provider."""
        ...

    async def reconnect(self) -> None:
        """Attempt to re-establish a dropped connection."""
        ...

    async def shutdown(self) -> None:
        """Gracefully release provider connection resource pools."""
        ...
