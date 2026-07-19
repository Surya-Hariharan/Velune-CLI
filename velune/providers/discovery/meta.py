"""Meta Llama API model discovery."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.keystore import get_key


class MetaDiscovery:
    """Returns the Meta Llama API model list when a key is configured."""

    provider_id = "meta"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("meta"):
            return []

        from velune.providers.adapters.meta import META_MODELS

        return META_MODELS
