"""Provider abstraction layer."""

# Provider adapters: import from velune.providers.adapters.*
# velune/providers/{name}/provider.py files are deprecated shims

from velune.providers.base import EmbeddingProvider, InferenceEngine, ModelProvider
from velune.providers.benchmarker import ProviderBenchmarker
from velune.providers.registry import ProviderRegistry
from velune.providers.router import ProviderRouter
from velune.providers.task_classifier import TaskClassifier, TaskProfile, TaskType, ComplexityLevel

__all__ = [
    "ModelProvider",
    "InferenceEngine",
    "EmbeddingProvider",
    "ProviderRegistry",
    "ProviderRouter",
    "ProviderBenchmarker",
    "TaskClassifier",
    "TaskProfile",
    "TaskType",
    "ComplexityLevel",
]
