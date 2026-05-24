"""Provider abstraction layer."""

# Provider adapters: import from velune.providers.adapters.*
# velune/providers/{name}/provider.py files are deprecated shims

from velune.providers.base import ModelProvider, InferenceEngine, EmbeddingProvider
from velune.providers.registry import ProviderRegistry
from velune.providers.router import ProviderRouter
from velune.providers.benchmarker import ProviderBenchmarker

__all__ = [
    "ModelProvider",
    "InferenceEngine",
    "EmbeddingProvider",
    "ProviderRegistry",
    "ProviderRouter",
    "ProviderBenchmarker",
]
