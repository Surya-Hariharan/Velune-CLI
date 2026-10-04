"""Groq model discovery — the curated catalog reconciled with Groq's live /models."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers import keystore


class GroqDiscovery:
    """Returns Groq models (live-reconciled) when a key is configured."""

    provider_id = "groq"

    async def discover(self) -> list[ModelDescriptor]:
        # Call through the module (not a from-imported name) so the check
        # always reflects the current keystore state and stays patchable.
        if not keystore.has_key("groq"):
            return []
        from velune.providers.adapters.groq import fetch_live_models, reconcile_with_live

        live = await fetch_live_models(keystore.get_key("groq"), "https://api.groq.com/openai/v1")
        return reconcile_with_live(live)
