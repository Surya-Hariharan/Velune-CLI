"""NVIDIA NIM model discovery.

Two sources:
1. Cloud NIM — ``https://integrate.api.nvidia.com/v1/models`` when ``NVIDIA_API_KEY`` is set.
2. Local NIM container — ``http://localhost:8000/v1/models`` when a container is running
   and responding with NVIDIA-style model names (``nvidia/``, ``meta/``, ``mistral/`` prefixes).
"""

from __future__ import annotations

import logging

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger("velune.providers.discovery.nvidia_nim")

_CLOUD_BASE_URL = "https://integrate.api.nvidia.com/v1"
_LOCAL_BASE_URL = "http://localhost:8000/v1"

# Vendor prefixes that identify a local NIM container's model list
_NIM_VENDOR_PREFIXES = ("nvidia/", "meta/", "mistral/", "google/", "microsoft/", "deepseek/")


class NVIDIANIMDiscovery:
    """Discovers NVIDIA NIM models from cloud API and local containers.

    ``provider_id`` is ``"nvidia"`` — the same id the key is stored/validated
    under (``velune provider add nvidia``) — so ``ModelDiscoveryScanner``'s
    ``has_key()`` gate and ``scan_provider("nvidia")`` both actually match this
    discoverer. It was previously ``"nvidia_nim"``, an id nothing ever saves a
    key under, which silently made cloud NIM discovery a dead code path: the
    scanner's ``_should_run()`` always found no key and skipped it entirely.
    Per-model ``ModelDescriptor.provider_id`` values stay ``"nvidia_nim"`` /
    ``"nvidia_nim_local"`` (see ``_build_descriptor``) — unrelated to this.
    """

    provider_id = "nvidia"

    async def discover(self) -> list[ModelDescriptor]:
        from velune.providers.keystore import get_key

        key = get_key("nvidia")
        cloud: list[ModelDescriptor] = []
        local: list[ModelDescriptor] = []

        if key:
            cloud = await self._discover_cloud(key)

        local = await self._discover_local()

        # De-duplicate: cloud wins on same model_id
        seen = {m.model_id for m in cloud}
        unique_local = [m for m in local if m.model_id not in seen]
        return cloud + unique_local

    # ------------------------------------------------------------------
    # Cloud NIM
    # ------------------------------------------------------------------

    async def _discover_cloud(self, key: str) -> list[ModelDescriptor]:
        models: list[ModelDescriptor] = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{_CLOUD_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
                data = r.json()
                for item in data.get("data", []):
                    d = self._build_descriptor(item, location="cloud", is_local=False)
                    if d:
                        models.append(d)
        except Exception as e:
            logger.debug("NVIDIA NIM cloud discovery failed: %s", e)
        return models

    # ------------------------------------------------------------------
    # Local NIM container
    # ------------------------------------------------------------------

    async def _discover_local(self) -> list[ModelDescriptor]:
        models: list[ModelDescriptor] = []
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{_LOCAL_BASE_URL}/models")
                if r.status_code != 200:
                    return models
                data = r.json()
                items = data.get("data", [])
                # Only claim this endpoint if it looks like a NIM container
                if not any(i.get("id", "").startswith(_NIM_VENDOR_PREFIXES) for i in items):
                    return models
                for item in items:
                    d = self._build_descriptor(
                        item, location=_LOCAL_BASE_URL.removesuffix("/v1"), is_local=True
                    )
                    if d:
                        models.append(d)
        except Exception as e:
            logger.debug("NVIDIA NIM local discovery failed: %s", e)
        return models

    # ------------------------------------------------------------------
    # Descriptor construction
    # ------------------------------------------------------------------

    def _build_descriptor(
        self, item: dict, location: str, is_local: bool
    ) -> ModelDescriptor | None:
        model_id = item.get("id")
        if not model_id:
            return None

        capabilities = self._classify_capabilities(model_id)
        tags = ["nvidia-nim", "local" if is_local else "cloud"]
        if capabilities.vision > CapabilityLevel.NONE:
            tags.append("vision")
        if capabilities.embedding > CapabilityLevel.NONE:
            tags.append("embedding")

        return ModelDescriptor(
            model_id=model_id,
            provider_id="nvidia_nim" if not is_local else "nvidia_nim_local",
            display_name=model_id.split("/")[-1] if "/" in model_id else model_id,
            context_length=item.get("context_window", 131072),
            capabilities=capabilities,
            is_local=is_local,
            speed_tier="fast",
            cost_per_1k_tokens=None if is_local else self._estimate_cost(model_id),
            tags=tags,
            location=location,
            metadata={"raw": item, "source": "local" if is_local else "cloud"},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        lower = model_id.lower()
        profile = ModelCapabilityProfile()

        # Vision
        if any(kw in lower for kw in ["vision", "llava", "vl", "vlm", "minicpm-v", "moondream"]):
            profile.vision = CapabilityLevel.ADVANCED
            profile.multimodal = CapabilityLevel.ADVANCED

        # Embedding
        if any(kw in lower for kw in ["embed", "e5-", "bge-", "gte-", "nv-embed"]):
            profile.embedding = CapabilityLevel.EXPERT
            return profile  # embedding models don't have chat capabilities

        # Reasoning
        if any(kw in lower for kw in ["r1", "reason", "qwq", "deepseek-r1"]):
            profile.reasoning = CapabilityLevel.EXPERT
            profile.planning = CapabilityLevel.ADVANCED
        elif any(kw in lower for kw in ["llama-3", "mistral", "nemotron"]):
            profile.reasoning = CapabilityLevel.ADVANCED

        # Coding
        if any(kw in lower for kw in ["coder", "code", "starcoder", "deepseek-coder"]):
            profile.coding = CapabilityLevel.ADVANCED
        else:
            profile.coding = CapabilityLevel.INTERMEDIATE

        # Tool use
        if any(kw in lower for kw in ["instruct", "chat", "nemotron", "llama-3"]):
            profile.tool_use = CapabilityLevel.ADVANCED
            profile.instruction_following = CapabilityLevel.ADVANCED

        # Long context
        if any(kw in lower for kw in ["128k", "200k", "1m"]):
            profile.long_context = CapabilityLevel.EXPERT

        return profile

    def _estimate_cost(self, model_id: str) -> float | None:
        lower = model_id.lower()
        if "llama-3.1-405b" in lower or "nemotron-340b" in lower:
            return 0.005
        if any(kw in lower for kw in ["70b", "72b", "8x7b"]):
            return 0.001
        if any(kw in lower for kw in ["8b", "7b", "13b"]):
            return 0.0002
        return None
