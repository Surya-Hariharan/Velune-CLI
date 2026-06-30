"""Model discovery components.

Symbols are resolved lazily (PEP 562) so that merely importing this package —
which happens on the Tier-0 startup path via ``velune.providers.discovery.scanner``
— does not eagerly import every discovery backend (each cloud backend pulls in
``httpx``). The classes are imported only when actually accessed, e.g.
``from velune.providers.discovery import OllamaDiscovery``.
"""

from __future__ import annotations

# Map public symbol -> submodule that defines it. Used by ``__getattr__`` to
# import on first access only.
_LAZY: dict[str, str] = {
    "ModelDiscoveryScanner": "scanner",
    "OllamaDiscovery": "ollama",
    "LMStudioDiscovery": "lmstudio",
    "GGUFDiscovery": "gguf",
    "DockerDiscovery": "docker",
    "NVIDIANIMDiscovery": "nvidia_nim",
    "OpenAICompatDiscovery": "openai_compat",
    "HuggingFaceDiscovery": "huggingface",
    "OpenAIDiscovery": "openai",
    "AnthropicDiscovery": "anthropic",
    "XAIDiscovery": "xai",
    "GoogleDiscovery": "google",
    "GroqDiscovery": "groq",
    "OpenRouterDiscovery": "openrouter",
    "GPUDetector": "gpu",
    "CapabilityClassifier": "classifier",
    "CapabilityBenchmark": "benchmarks",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    obj = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = obj  # cache so subsequent access skips __getattr__
    return obj


def __dir__() -> list[str]:
    return sorted(__all__)
