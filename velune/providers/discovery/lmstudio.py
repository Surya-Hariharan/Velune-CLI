from __future__ import annotations

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor


class LMStudioDiscovery:
    """Discovers models from LM Studio."""

    def __init__(self):
        self.provider_id = "lmstudio"
        self.base_url = "http://localhost:1234"

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
        except Exception:
            pass

        return models

    def _parse_model(self, model_data: dict) -> ModelDescriptor:
        """Parse model data into descriptor."""
        model_id = model_data["id"]

        # LM Studio doesn't provide detailed info, use heuristics
        capabilities = self._classify_capabilities(model_id)

        return ModelDescriptor(
            model_id=model_id,
            provider_id=self.provider_id,
            display_name=model_id,
            context_length=4096,  # Default
            capabilities=capabilities,
            quantization=None,
            vram_required_gb=None,
            parameter_count_b=None,
            speed_tier="medium",
            cost_per_1k_tokens=None,
            tags=["local", "lmstudio"],
            metadata={"raw": model_data},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        """Classify model capabilities based on name."""
        model_lower = model_id.lower()

        profile = ModelCapabilityProfile()

        # Basic capability classification
        if any(name in model_lower for name in ["coder", "code"]):
            profile.coding = CapabilityLevel.INTERMEDIATE
        else:
            profile.coding = CapabilityLevel.BASIC

        profile.reasoning = CapabilityLevel.BASIC
        profile.instruction_following = CapabilityLevel.INTERMEDIATE
        profile.summarization = CapabilityLevel.BASIC

        return profile
