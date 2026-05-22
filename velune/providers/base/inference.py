"""Inference abstraction layer."""

from abc import ABC, abstractmethod
from typing import AsyncIterator
from velune.core.types import InferenceRequest, InferenceResponse, StreamChunk


class InferenceEngine(ABC):
    """Abstract inference engine."""

    @abstractmethod
    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Perform inference."""
        pass

    @abstractmethod
    async def infer_stream(
        self, request: InferenceRequest
    ) -> AsyncIterator[StreamChunk]:
        """Perform streaming inference."""
        pass
