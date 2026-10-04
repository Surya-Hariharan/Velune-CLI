"""Anthropic model discovery: curated Claude catalog reconciled with the live /v1/models list."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.adapters.anthropic import discover_anthropic_models
from velune.providers.keystore import get_key


class AnthropicDiscovery:
    """Discovers the Claude models an Anthropic key can actually call."""

    provider_id = "anthropic"

    def __init__(self) -> None:
        self.api_key = get_key("anthropic")
        self.base_url = "https://api.anthropic.com"

    async def discover(self) -> list[ModelDescriptor]:
        if not self.api_key:
            return []
        return await discover_anthropic_models(self.api_key, self.base_url)
