"""Provider abstraction layer."""

# Provider adapters: import from velune.providers.adapters.*
# velune/providers/{name}/provider.py files are deprecated shims

from velune.providers.base import EmbeddingProvider, InferenceEngine, ModelProvider
from velune.providers.benchmarker import ProviderBenchmarker
from velune.providers.registry import ProviderRegistry
from velune.providers.router import ProviderRouter

__all__ = [
    "ModelProvider",
    "InferenceEngine",
    "EmbeddingProvider",
    "ProviderRegistry",
    "ProviderRouter",
    "ProviderBenchmarker",
]
