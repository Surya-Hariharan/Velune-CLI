"""Provider registry."""

from typing import Dict, Optional
from velune.providers.base import ModelProvider
from velune.providers.ollama import OllamaProvider
from velune.providers.openai import OpenAIProvider
from velune.providers.anthropic import AnthropicProvider
from velune.core.errors import ProviderNotFoundError


class ProviderRegistry:
    """Registry for model providers."""

    def __init__(self):
        self._providers: Dict[str, ModelProvider] = {}
        self._register_default_providers()

    def _register_default_providers(self) -> None:
        """Register default providers."""
        self.register("ollama", OllamaProvider())
        self.register("openai", OpenAIProvider())
        self.register("anthropic", AnthropicProvider())

    def register(self, name: str, provider: ModelProvider) -> None:
        """Register a provider."""
        self._providers[name] = provider

    def get(self, name: str) -> Optional[ModelProvider]:
        """Get a provider by name."""
        return self._providers.get(name)

    def get_or_raise(self, name: str) -> ModelProvider:
        """Get a provider by name or raise an error."""
        provider = self.get(name)
        if not provider:
            raise ProviderNotFoundError(f"Provider not found: {name}")
        return provider

    def list_providers(self) -> list[str]:
        """List all registered provider names."""
        return list(self._providers.keys())

    async def initialize_all(self) -> None:
        """Initialize all providers."""
        for provider in self._providers.values():
            await provider.initialize()

    async def shutdown_all(self) -> None:
        """Shutdown all providers."""
        for provider in self._providers.values():
            await provider.shutdown()
