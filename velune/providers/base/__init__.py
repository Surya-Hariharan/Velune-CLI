"""Provider base protocols."""

from velune.providers.base.provider import ModelProvider
from velune.providers.base.inference import InferenceEngine
from velune.providers.base.embedding import EmbeddingProvider

__all__ = [
    "ModelProvider",
    "InferenceEngine",
    "EmbeddingProvider",
]
