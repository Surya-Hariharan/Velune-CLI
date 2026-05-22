"""Model discovery components."""

from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.providers.discovery.ollama import OllamaDiscovery
from velune.providers.discovery.lmstudio import LMStudioDiscovery
from velune.providers.discovery.gguf import GGUFDiscovery
from velune.providers.discovery.huggingface import HuggingFaceDiscovery
from velune.providers.discovery.openai import OpenAIDiscovery
from velune.providers.discovery.anthropic import AnthropicDiscovery
from velune.providers.discovery.gpu import GPUDetector
from velune.providers.discovery.classifier import CapabilityClassifier
from velune.providers.discovery.benchmarks import CapabilityBenchmark

__all__ = [
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
