"""OpenAI model discovery: the curated catalog reconciled with the account's live models."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.adapters._live_catalog import reconcile
from velune.providers.adapters.openai import (
    OPENAI_MODELS,
    fetch_live_models,
    is_openai_chat_model,
    openai_context_window,
)
from velune.providers.keystore import get_key


class OpenAIDiscovery:
    """Discovers the chat models an OpenAI key can actually use."""

    provider_id = "openai"

    def __init__(self) -> None:
        self.api_key = get_key("openai")
        self.base_url = "https://api.openai.com/v1"

    async def discover(self) -> list[ModelDescriptor]:
        if not self.api_key:
            return []
        live = await fetch_live_models(self.api_key, self.base_url)
        return reconcile(
            OPENAI_MODELS,
            live,
            "openai",
            accept=is_openai_chat_model,
            fallback_window=openai_context_window,
            is_strong=lambda mid: openai_context_window(mid) >= 128000,
        )
