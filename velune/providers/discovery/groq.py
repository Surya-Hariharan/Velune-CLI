"""Groq model discovery — returns GROQ_MODELS when a key is configured."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.keystore import has_key


class GroqDiscovery:
    """Returns the hardcoded Groq model list when a key is configured."""

    provider_id = "groq"

    async def discover(self) -> list[ModelDescriptor]:
        if not has_key("groq"):
            return []
        from velune.providers.adapters.groq import GROQ_MODELS
        return GROQ_MODELS
