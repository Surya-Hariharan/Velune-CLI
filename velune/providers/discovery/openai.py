"""OpenAI model discovery: the curated catalog reconciled with the account's live models."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.adapters._live_catalog import as_fallback, last_listing_failure, reconcile
from velune.providers.adapters.openai import (
    OPENAI_MODELS,
    fetch_live_models,
    is_openai_chat_model,
    openai_context_window,
)
from velune.providers.keystore import get_key


class OpenAIDiscovery:
    """Discovers the chat models an OpenAI key can actually use.

    The key and base URL are read on every :meth:`discover`, not at
    construction: the scanner caches discoverers for the life of the process,
    so a key added after the first scan used to yield nothing until restart.
    """

    provider_id = "openai"

    def __init__(self) -> None:
        #: Why the last listing failed (None = it succeeded).
        self.last_error: str | None = None

    @property
    def base_url(self) -> str:
        from velune.providers.endpoints import provider_base_url

        return provider_base_url("openai")

    async def discover(self) -> list[ModelDescriptor]:
        self.last_error = None
        api_key = get_key("openai")
        if not api_key:
            return []
        live = await fetch_live_models(api_key, self.base_url)
        if live is None:
            failure = last_listing_failure()
            self.last_error = failure.describe() if failure else "no response"
            return as_fallback(OPENAI_MODELS, failure)
        return reconcile(
            OPENAI_MODELS,
            live,
            "openai",
            accept=is_openai_chat_model,
            fallback_window=openai_context_window,
            is_strong=lambda mid: openai_context_window(mid) >= 128000,
        )
