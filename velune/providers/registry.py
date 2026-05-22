"""Provider registry."""

from __future__ import annotations

import importlib
from typing import Callable, Dict, Optional

from velune.core.errors import ProviderNotFoundError
from velune.core.config.schema import ProvidersConfig
from velune.providers.base import ModelProvider


class ProviderRegistry:
    """Registry for model providers."""

    def __init__(self, config: ProvidersConfig | None = None):
        self._providers: Dict[str, ModelProvider] = {}
        self._factories: Dict[str, Callable[[], ModelProvider]] = {}
        self._config = config
        self._register_default_providers()

    def _register_default_providers(self) -> None:
        """Register default providers."""
        if self._config and self._config.ollama:
            self.register_factory(
                "ollama",
                self._provider_factory(
                    "velune.providers.ollama.provider",
                    "OllamaProvider",
                    base_url=self._config.ollama.base_url,
                ),
            )
        else:
            self.register_factory(
                "ollama",
                self._provider_factory(
                    "velune.providers.ollama.provider",
                    "OllamaProvider",
                ),
            )

        if self._config and self._config.openai:
            self.register_factory(
                "openai",
                self._provider_factory(
                    "velune.providers.openai.provider",
                    "OpenAIProvider",
                    base_url=self._config.openai.base_url,
                ),
            )
        else:
            self.register_factory(
                "openai",
                self._provider_factory(
                    "velune.providers.openai.provider",
                    "OpenAIProvider",
                ),
            )

        if self._config and self._config.anthropic:
            self.register_factory(
                "anthropic",
                self._provider_factory(
                    "velune.providers.anthropic.provider",
                    "AnthropicProvider",
                    base_url=self._config.anthropic.base_url,
                ),
            )
        else:
            self.register_factory(
                "anthropic",
                self._provider_factory(
                    "velune.providers.anthropic.provider",
                    "AnthropicProvider",
                ),
            )

    def _provider_factory(self, module_path: str, class_name: str, **kwargs) -> Callable[[], ModelProvider]:
        """Create a lazy provider factory."""

        def factory() -> ModelProvider:
            module = importlib.import_module(module_path)
            provider_class = getattr(module, class_name)
            return provider_class(**kwargs)

        return factory

    def register(self, name: str, provider: ModelProvider) -> None:
        """Register a provider."""
        self._providers[name] = provider

    def register_factory(self, name: str, factory: Callable[[], ModelProvider]) -> None:
        """Register a lazy provider factory."""

        self._factories[name] = factory

    def get(self, name: str) -> Optional[ModelProvider]:
        """Get a provider by name."""
        if name in self._providers:
            return self._providers[name]
        if name in self._factories:
            provider = self._factories[name]()
            self._providers[name] = provider
            return provider
        return None

    def get_or_raise(self, name: str) -> ModelProvider:
        """Get a provider by name or raise an error."""
        provider = self.get(name)
        if not provider:
            raise ProviderNotFoundError(f"Provider not found: {name}")
        return provider

    def list_providers(self) -> list[str]:
        """List all registered provider names."""
        return sorted(set(self._providers.keys()) | set(self._factories.keys()))

    async def initialize_all(self) -> None:
        """Initialize all providers."""
        for provider_name in self.list_providers():
            provider = self.get_or_raise(provider_name)
            await provider.initialize()

    async def shutdown_all(self) -> None:
        """Shutdown all providers."""
        for provider in self._providers.values():
            await provider.shutdown()
