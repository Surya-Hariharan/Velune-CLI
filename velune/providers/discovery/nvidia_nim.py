"""NVIDIA NIM model discovery (hosted API at ``integrate.api.nvidia.com``).

Every model is registered under ``provider_id="nvidia"`` — the id the key is
stored, validated and routed under. Models used to be tagged ``nvidia_nim``,
which no registry factory, catalog entry or keystore record knew: they showed
up in ``/model`` but were filtered out of the switcher, palette and council,
and failed with ``ProviderNotFoundError`` when activated, even with a valid key.

A self-hosted NIM container is an OpenAI-compatible server and is found by
:mod:`velune.providers.discovery.openai_compat` (it scans the same ports this
module used to); scanning it here as well only produced duplicates under an
id with no adapter.
"""

from __future__ import annotations

import logging

import httpx

from velune.core.types.model import ModelDescriptor
from velune.providers.adapters.nvidia import is_nim_chat_model, nim_capabilities

logger = logging.getLogger("velune.providers.discovery.nvidia_nim")


class NVIDIANIMDiscovery:
    """Discovers the chat models served by NVIDIA's hosted NIM API."""

    provider_id = "nvidia"

    def __init__(self) -> None:
        #: Why the last :meth:`discover` could not list models (None = it could).
        self.last_error: str | None = None

    async def discover(self) -> list[ModelDescriptor]:
        from velune.providers.endpoints import provider_base_url
        from velune.providers.keystore import get_key

        self.last_error = None
        key = get_key("nvidia")
        if not key:
            return []
        base = provider_base_url("nvidia")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
                return []
            data = r.json()
        except httpx.TimeoutException:
            self.last_error = "connection timed out"
            return []
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = f"could not connect ({type(e).__name__})"
            logger.debug("NVIDIA NIM discovery failed: %s", e)
            return []

        models: list[ModelDescriptor] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            d = self._build_descriptor(item)
            if d is not None:
                models.append(d)
        return models

    def _build_descriptor(self, item: dict) -> ModelDescriptor | None:
        model_id = item.get("id") if isinstance(item, dict) else None
        if not model_id or not is_nim_chat_model(model_id):
            return None
        capabilities = nim_capabilities(model_id)
        tags = ["nvidia-nim", "cloud"]
        if capabilities.vision:
            tags.append("vision")
        return ModelDescriptor(
            model_id=model_id,
            provider_id="nvidia",
            display_name=model_id.split("/")[-1],
            context_length=int(item.get("context_window") or 131072),
            capabilities=capabilities,
            is_local=False,
            speed_tier="fast",
            cost_per_1k_tokens=self._estimate_cost(model_id),
            tags=tags,
            location="cloud",
            metadata={"owned_by": item.get("owned_by"), "source": "live"},
        )

    def _estimate_cost(self, model_id: str) -> float | None:
        lower = model_id.lower()
        if "llama-3.1-405b" in lower or "nemotron-340b" in lower:
            return 0.005
        if any(kw in lower for kw in ["70b", "72b", "8x7b"]):
            return 0.001
        if any(kw in lower for kw in ["8b", "7b", "13b"]):
            return 0.0002
        return None
