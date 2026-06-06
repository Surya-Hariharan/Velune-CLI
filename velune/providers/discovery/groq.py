"""Groq model discovery — returns GROQ_MODELS when a key is configured."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers import keystore


class GroqDiscovery:
    """Returns the hardcoded Groq model list when a key is configured."""

    provider_id = "groq"

    async def discover(self) -> list[ModelDescriptor]:
        # Call through the module (not a from-imported name) so the check
        # always reflects the current keystore state and stays patchable.
        if not keystore.has_key("groq"):
            return []
        from velune.providers.adapters.groq import GROQ_MODELS
        return GROQ_MODELS
