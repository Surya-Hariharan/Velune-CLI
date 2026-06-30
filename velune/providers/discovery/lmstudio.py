from __future__ import annotations

import logging

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger("velune.providers.discovery.lmstudio")


class LMStudioDiscovery:
    """Discovers models from LM Studio."""

    def __init__(self):
        self.provider_id = "lmstudio"
        self.base_url = "http://localhost:1234"

    @classmethod
    async def is_running(cls) -> bool:
        """Return True if the LM Studio server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.head("http://localhost:1234")
                return r.status_code < 500
        except Exception:
            return False

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from LM Studio."""
        models = []

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                response.raise_for_status()
                data = response.json()

                for model in data.get("data", []):
                    descriptor = self._parse_model(model)
                    if descriptor:
                        models.append(descriptor)
        except Exception as e:
            logger.debug("LM Studio discovery failed: %s", e)

        return models

    def _parse_model(self, model_data: dict) -> ModelDescriptor:
        model_id = model_data["id"]
        capabilities = self._classify_capabilities(model_id)

        return ModelDescriptor(
            model_id=model_id,
            provider_id=self.provider_id,
            display_name=model_id,
            context_length=4096,
            capabilities=capabilities,
            quantization=None,
            vram_required_gb=None,
            parameter_count_b=None,
            speed_tier="medium",
            cost_per_1k_tokens=None,
            location=self.base_url,
            health="unknown",
            tags=["local", "lmstudio"],
            metadata={"raw": model_data},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        model_lower = model_id.lower()
        profile = ModelCapabilityProfile()

        if any(name in model_lower for name in ["coder", "code", "starcoder"]):
            profile.coding = CapabilityLevel.INTERMEDIATE
        else:
            profile.coding = CapabilityLevel.BASIC

        if any(name in model_lower for name in ["r1", "reason", "qwq"]):
            profile.reasoning = CapabilityLevel.ADVANCED
        else:
            profile.reasoning = CapabilityLevel.BASIC

        profile.instruction_following = CapabilityLevel.INTERMEDIATE
        profile.summarization = CapabilityLevel.BASIC

        if any(name in model_lower for name in ["llava", "vision", "vl", "moondream", "minicpm-v", "bakllava"]):
            profile.vision = CapabilityLevel.ADVANCED
            profile.multimodal = CapabilityLevel.ADVANCED

        if any(name in model_lower for name in ["embed", "bge-", "e5-", "gte-"]):
            profile.embedding = CapabilityLevel.ADVANCED

        if any(name in model_lower for name in ["instruct", "chat"]):
            profile.tool_use = CapabilityLevel.INTERMEDIATE

        return profile
