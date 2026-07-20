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
    "MetaDiscovery": "meta",
    "GPUDetector": "gpu",
}

import typing

if typing.TYPE_CHECKING:
    from velune.providers.discovery.anthropic import AnthropicDiscovery as AnthropicDiscovery
    from velune.providers.discovery.docker import DockerDiscovery as DockerDiscovery
    from velune.providers.discovery.gguf import GGUFDiscovery as GGUFDiscovery
    from velune.providers.discovery.google import GoogleDiscovery as GoogleDiscovery
    from velune.providers.discovery.gpu import GPUDetector as GPUDetector
    from velune.providers.discovery.groq import GroqDiscovery as GroqDiscovery
    from velune.providers.discovery.huggingface import HuggingFaceDiscovery as HuggingFaceDiscovery
    from velune.providers.discovery.lmstudio import LMStudioDiscovery as LMStudioDiscovery
    from velune.providers.discovery.meta import MetaDiscovery as MetaDiscovery
    from velune.providers.discovery.nvidia_nim import NVIDIANIMDiscovery as NVIDIANIMDiscovery
    from velune.providers.discovery.ollama import OllamaDiscovery as OllamaDiscovery
    from velune.providers.discovery.openai import OpenAIDiscovery as OpenAIDiscovery
    from velune.providers.discovery.openai_compat import (
        OpenAICompatDiscovery as OpenAICompatDiscovery,
    )
    from velune.providers.discovery.openrouter import OpenRouterDiscovery as OpenRouterDiscovery
    from velune.providers.discovery.scanner import ModelDiscoveryScanner as ModelDiscoveryScanner
    from velune.providers.discovery.xai import XAIDiscovery as XAIDiscovery

__all__ = [
    "ModelDiscoveryScanner",
    "OllamaDiscovery",
    "LMStudioDiscovery",
    "GGUFDiscovery",
    "DockerDiscovery",
    "NVIDIANIMDiscovery",
    "OpenAICompatDiscovery",
    "HuggingFaceDiscovery",
    "OpenAIDiscovery",
    "AnthropicDiscovery",
    "XAIDiscovery",
    "GoogleDiscovery",
    "GroqDiscovery",
    "OpenRouterDiscovery",
    "MetaDiscovery",
    "GPUDetector",
]


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
