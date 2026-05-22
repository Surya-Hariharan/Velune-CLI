"""Model provider protocol."""

from typing import Protocol, AsyncIterator, runtime_checkable
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import ModelDescriptor
from velune.core.types.provider import ProviderCapabilities
from velune.core.types.provider import ProviderHealth


@runtime_checkable
class ModelProvider(Protocol):
    """Core provider contract. All providers implement this."""

    @property
    def provider_id(self) -> str: ...

    async def list_models(self) -> list[ModelDescriptor]:
        """Enumerate all available models from this provider."""
        ...

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Single-turn inference."""
        ...

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Streaming inference."""
        ...

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        """Generate embeddings. Raises NotImplementedError if unsupported."""
        ...

    async def health_check(self) -> ProviderHealth:
        """Verify provider connectivity and model availability."""
        ...

    def get_capabilities(self) -> ProviderCapabilities:
        """Return static capability declarations."""
        ...
