"""Anthropic model discovery: curated Claude catalog reconciled with the live /v1/models list."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.adapters._live_catalog import as_fallback, last_listing_failure
from velune.providers.adapters.anthropic import ANTHROPIC_MODELS, discover_anthropic_models
from velune.providers.keystore import get_key


class AnthropicDiscovery:
    """Discovers the Claude models an Anthropic key can actually call.

    Reads the key and base URL per :meth:`discover` (see ``OpenAIDiscovery``).
    """

    provider_id = "anthropic"

    def __init__(self) -> None:
        #: Why the last listing failed (None = it succeeded).
        self.last_error: str | None = None

    @property
    def base_url(self) -> str:
        from velune.providers.endpoints import provider_base_url

        return provider_base_url("anthropic")

    async def discover(self) -> list[ModelDescriptor]:
        self.last_error = None
        api_key = get_key("anthropic")
        if not api_key:
            return []
        models = await discover_anthropic_models(api_key, self.base_url)
        failure = last_listing_failure()
        if failure is not None:
            self.last_error = failure.describe()
            return as_fallback(ANTHROPIC_MODELS, failure)
        return models
