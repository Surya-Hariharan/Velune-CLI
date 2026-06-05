"""Unified model discovery coordinator."""

from __future__ import annotations

import asyncio

from velune.core.types.model import ModelDescriptor
from velune.providers.discovery.anthropic import AnthropicDiscovery
from velune.providers.discovery.gguf import GGUFDiscovery
from velune.providers.discovery.google import GoogleDiscovery
from velune.providers.discovery.groq import GroqDiscovery
from velune.providers.discovery.huggingface import HuggingFaceDiscovery
from velune.providers.discovery.lmstudio import LMStudioDiscovery
from velune.providers.discovery.ollama import OllamaDiscovery
from velune.providers.discovery.openai import OpenAIDiscovery
from velune.providers.discovery.openrouter import OpenRouterDiscovery
from velune.providers.discovery.xai import XAIDiscovery

# Providers that run locally and need no API key
_LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "lmstudio", "gguf", "llamacpp"})


class ModelDiscoveryScanner:
    """Coordinates model discovery across all configured providers.

    Cloud discoverers are only invoked when a key is available for that
    provider.  Local discoverers always run.
    """

    def __init__(self) -> None:
        self.discoverers = [
            OllamaDiscovery(),
            LMStudioDiscovery(),
            GGUFDiscovery(),
            HuggingFaceDiscovery(),
            OpenAIDiscovery(),
            AnthropicDiscovery(),
            XAIDiscovery(),
            GoogleDiscovery(),
            GroqDiscovery(),
            OpenRouterDiscovery(),
        ]

    def _should_run(self, discoverer) -> bool:
        """Return True if this discoverer should be queried."""
        if discoverer.provider_id in _LOCAL_PROVIDERS:
            return True
        from velune.providers.keystore import has_key
        return has_key(discoverer.provider_id)

    async def scan_all(self) -> list[ModelDescriptor]:
        """Scan all providers for models in parallel, skipping cloud providers without keys."""
        tasks = [d.discover() for d in self.discoverers if self._should_run(d)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_models: list[ModelDescriptor] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            if isinstance(result, list):
                all_models.extend(result)
        return all_models

    async def scan_provider(self, provider_id: str) -> list[ModelDescriptor]:
        """Scan a specific provider by ID."""
        for discoverer in self.discoverers:
            if discoverer.provider_id == provider_id:
                return await discoverer.discover()
        return []
