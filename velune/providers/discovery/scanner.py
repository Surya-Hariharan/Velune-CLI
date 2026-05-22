"""Unified model discovery coordinator."""

from typing import List, Dict, Any
from velune.providers.discovery.ollama import OllamaDiscovery
from velune.providers.discovery.lmstudio import LMStudioDiscovery
from velune.providers.discovery.gguf import GGUFDiscovery
from velune.providers.discovery.huggingface import HuggingFaceDiscovery
from velune.providers.discovery.openai import OpenAIDiscovery
from velune.providers.discovery.anthropic import AnthropicDiscovery
from velune.core.types.model import ModelDescriptor


class ModelDiscoveryScanner:
    """Coordinates model discovery across all providers."""

    def __init__(self):
        self.discoverers = [
            OllamaDiscovery(),
            LMStudioDiscovery(),
            GGUFDiscovery(),
            HuggingFaceDiscovery(),
            OpenAIDiscovery(),
            AnthropicDiscovery(),
        ]

    async def scan_all(self) -> list[ModelDescriptor]:
        """Scan all providers for models in parallel."""
        import asyncio
        
        tasks = [discoverer.discover() for discoverer in self.discoverers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_models = []
        for result in results:
            if isinstance(result, Exception):
                continue
            if isinstance(result, list):
                all_models.extend(result)
        
        return all_models

    async def scan_provider(self, provider_id: str) -> list[ModelDescriptor]:
        """Scan a specific provider."""
        for discoverer in self.discoverers:
            if discoverer.provider_id == provider_id:
                return await discoverer.discover()
        return []
