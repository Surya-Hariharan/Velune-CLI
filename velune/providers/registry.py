"""Provider registry."""

from __future__ import annotations

import importlib
from collections.abc import Callable

from velune.core.errors import ProviderNotFoundError
from velune.kernel.config import ProvidersConfig
from velune.providers.base import ModelProvider


class ProviderRegistry:
    """Registry for model providers."""

    def __init__(self, config: ProvidersConfig | None = None):
        self._providers: dict[str, ModelProvider] = {}
        self._factories: dict[str, Callable[[], ModelProvider]] = {}

        self._config = config
        if (
            config is not None
            and not isinstance(config, ProvidersConfig)
            and hasattr(config, "providers")
        ):
            self._config = config.providers

        self._register_default_providers()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _provider_factory(
        self, module_path: str, class_name: str, **kwargs
    ) -> Callable[[], ModelProvider]:
        """Return a lazy factory that imports the adapter on first call."""

        def factory() -> ModelProvider:
            module = importlib.import_module(module_path)
            provider_class = getattr(module, class_name)
            return provider_class(**kwargs)

        return factory

    def _keyed_factory(
        self,
        module_path: str,
        class_name: str,
        provider_key: str,
        **kwargs,
    ) -> Callable[[], ModelProvider]:
        """Like _provider_factory but resolves the API key via keystore on each instantiation."""

        def factory() -> ModelProvider:
            from velune.providers.keystore import get_key

            module = importlib.import_module(module_path)
            provider_class = getattr(module, class_name)
            return provider_class(api_key=get_key(provider_key), **kwargs)

        return factory

    # ------------------------------------------------------------------
    # Provider registration
    # ------------------------------------------------------------------

    def _register_default_providers(self) -> None:
        cfg = self._config

        # ── Local / self-hosted ────────────────────────────────────────
        ollama_url = (
            cfg.ollama.base_url if cfg and cfg.ollama and cfg.ollama.base_url else None
        ) or "http://localhost:11434"
        self.register_factory(
            "ollama",
            self._provider_factory(
                "velune.providers.adapters.ollama", "OllamaProvider", base_url=ollama_url
            ),
        )

        lmstudio_url = (
            cfg.lmstudio.base_url if cfg and cfg.lmstudio and cfg.lmstudio.base_url else None
        ) or "http://localhost:1234/v1"
        self.register_factory(
            "lmstudio",
            self._provider_factory(
                "velune.providers.adapters.lmstudio", "LMStudioProvider", base_url=lmstudio_url
            ),
        )

        self.register_factory(
            "llamacpp",
            self._provider_factory("velune.providers.adapters.llamacpp", "LlamaCppProvider"),
        )

        # ── Cloud — key resolved via keystore at instantiation time ───
        openai_url = (
            cfg.openai.base_url if cfg and cfg.openai and cfg.openai.base_url else None
        ) or "https://api.openai.com/v1"
        self.register_factory(
            "openai",
            self._keyed_factory(
                "velune.providers.adapters.openai", "OpenAIProvider", "openai", base_url=openai_url
            ),
        )

        anthropic_url = (
            cfg.anthropic.base_url if cfg and cfg.anthropic and cfg.anthropic.base_url else None
        ) or "https://api.anthropic.com"
        self.register_factory(
            "anthropic",
            self._keyed_factory(
                "velune.providers.adapters.anthropic",
                "AnthropicProvider",
                "anthropic",
                base_url=anthropic_url,
            ),
        )

        hf_url = (
            cfg.huggingface.base_url
            if cfg and cfg.huggingface and cfg.huggingface.base_url
            else None
        ) or "https://api-inference.huggingface.co"
        self.register_factory(
            "huggingface",
            self._keyed_factory(
                "velune.providers.adapters.huggingface",
                "HuggingFaceProvider",
                "huggingface",
                base_url=hf_url,
            ),
        )

        self.register_factory(
            "xai",
            self._keyed_factory("velune.providers.adapters.xai", "XAIProvider", "xai"),
        )

        self.register_factory(
            "google",
            self._keyed_factory("velune.providers.adapters.google", "GoogleProvider", "google"),
        )

        self.register_factory(
            "groq",
            self._keyed_factory("velune.providers.adapters.groq", "GroqProvider", "groq"),
        )

        self.register_factory(
            "openrouter",
            self._keyed_factory(
                "velune.providers.adapters.openrouter", "OpenRouterProvider", "openrouter"
            ),
        )

        self.register_factory(
            "together",
            self._keyed_factory(
                "velune.providers.adapters.together", "TogetherProvider", "together"
            ),
        )

        self.register_factory(
            "fireworks",
            self._keyed_factory(
                "velune.providers.adapters.fireworks", "FireworksProvider", "fireworks"
            ),
        )

        self.register_factory(
            "deepseek",
            self._keyed_factory(
                "velune.providers.adapters.deepseek", "DeepSeekProvider", "deepseek"
            ),
        )

        self.register_factory(
            "mistral",
            self._keyed_factory("velune.providers.adapters.mistral", "MistralProvider", "mistral"),
        )

        self.register_factory(
            "cohere",
            self._keyed_factory("velune.providers.adapters.cohere", "CohereProvider", "cohere"),
        )

        self.register_factory(
            "nvidia",
            self._keyed_factory("velune.providers.adapters.nvidia", "NVIDIAProvider", "nvidia"),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, name: str, provider: ModelProvider) -> None:
        self._providers[name] = provider

    def register_factory(self, name: str, factory: Callable[[], ModelProvider]) -> None:
        self._factories[name] = factory

    def get(self, name: str) -> ModelProvider | None:
        if name in self._providers:
            return self._providers[name]
        if name in self._factories:
            provider = self._factories[name]()
            self._providers[name] = provider
            return provider
        return None

    def get_or_raise(self, name: str) -> ModelProvider:
        provider = self.get(name)
        if not provider:
            raise ProviderNotFoundError(f"Provider not found: {name}")
        return provider

    def list_providers(self) -> list[str]:
        """Return all registered provider names (sorted)."""
        return sorted(set(self._providers.keys()) | set(self._factories.keys()))

    def check_provider_available(self, provider_id: str) -> bool:
        """Return True if *provider_id* has an API key configured in keystore."""
        from velune.providers.keystore import has_key

        return has_key(provider_id)

    def list_available_providers(self) -> list[str]:
        """Return registered cloud providers that have a key configured."""
        from velune.providers.keystore import has_key

        return sorted(p for p in self.list_providers() if has_key(p))

    async def initialize_all(self) -> None:
        for provider_name in self.list_providers():
            provider = self.get_or_raise(provider_name)
            await provider.initialize()

    async def shutdown_all(self) -> None:
        for provider in self._providers.values():
            await provider.shutdown()
