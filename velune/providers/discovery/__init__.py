"""Model discovery components."""

from velune.providers.discovery.anthropic import AnthropicDiscovery
from velune.providers.discovery.benchmarks import CapabilityBenchmark
from velune.providers.discovery.classifier import CapabilityClassifier
from velune.providers.discovery.gguf import GGUFDiscovery
from velune.providers.discovery.google import GoogleDiscovery
from velune.providers.discovery.gpu import GPUDetector
from velune.providers.discovery.groq import GroqDiscovery
from velune.providers.discovery.huggingface import HuggingFaceDiscovery
from velune.providers.discovery.lmstudio import LMStudioDiscovery
from velune.providers.discovery.ollama import OllamaDiscovery
from velune.providers.discovery.openai import OpenAIDiscovery
from velune.providers.discovery.openrouter import OpenRouterDiscovery
from velune.providers.discovery.scanner import ModelDiscoveryScanner
from velune.providers.discovery.xai import XAIDiscovery

__all__ = [
    "ModelDiscoveryScanner",
    "OllamaDiscovery",
    "LMStudioDiscovery",
    "GGUFDiscovery",
    "HuggingFaceDiscovery",
    "OpenAIDiscovery",
    "AnthropicDiscovery",
    "XAIDiscovery",
    "GoogleDiscovery",
    "GroqDiscovery",
    "OpenRouterDiscovery",
    "GPUDetector",
    "CapabilityClassifier",
    "CapabilityBenchmark",
]
