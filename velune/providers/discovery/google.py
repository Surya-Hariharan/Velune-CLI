"""Google Gemini model discovery: curated catalog reconciled with the live models list."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.adapters.google import discover_gemini_models
from velune.providers.keystore import get_key


class GoogleDiscovery:
    """Discovers the Gemini models a Google API key can actually call."""

    provider_id = "google"

    async def discover(self) -> list[ModelDescriptor]:
        key = get_key("google")
        if not key:
            return []
        return await discover_gemini_models(key)
