"""Provider abstraction layer."""

from velune.providers.base import ModelProvider, InferenceEngine, EmbeddingProvider
from velune.providers.ollama import OllamaProvider
from velune.providers.openai import OpenAIProvider
from velune.providers.anthropic import AnthropicProvider
from velune.providers.registry import ProviderRegistry
from velune.providers.discovery import (
    ModelDiscoveryScanner,
    OllamaDiscovery,
    LMStudioDiscovery,
    GGUFDiscovery,
    HuggingFaceDiscovery,
    OpenAIDiscovery,
    AnthropicDiscovery,
    GPUDetector,
    CapabilityClassifier,
    CapabilityBenchmark,
)

__all__ = [
    "ModelProvider",
    "InferenceEngine",
    "EmbeddingProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "ProviderRegistry",
    "ModelDiscoveryScanner",
    "OllamaDiscovery",
    "LMStudioDiscovery",
    "GGUFDiscovery",
    "HuggingFaceDiscovery",
    "OpenAIDiscovery",
    "AnthropicDiscovery",
    "GPUDetector",
    "CapabilityClassifier",
    "CapabilityBenchmark",
]
