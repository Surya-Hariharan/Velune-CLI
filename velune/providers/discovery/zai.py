"""Z.ai (Zhipu GLM) model discovery."""

from __future__ import annotations

from velune.core.types.model import ModelDescriptor
from velune.providers.keystore import get_key


class ZaiDiscovery:
    """Returns the Z.ai GLM model list when a key is configured."""

    provider_id = "zai"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("zai"):
            return []

        from velune.providers.adapters.zai import ZAI_MODELS

        return ZAI_MODELS
