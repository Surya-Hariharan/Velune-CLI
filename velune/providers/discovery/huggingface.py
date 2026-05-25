from __future__ import annotations

from pathlib import Path

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor


class HuggingFaceDiscovery:
    """Discovers models from HuggingFace cache."""

    def __init__(self):
        self.provider_id = "huggingface"
        self.cache_path = Path.home() / ".cache" / "huggingface" / "hub"

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from HuggingFace cache."""
        models = []

        if not self.cache_path.exists():
            return models

        # Look for model directories
        for model_dir in self.cache_path.iterdir():
            if not model_dir.is_dir():
                continue

            descriptor = self._parse_model_dir(model_dir)
            if descriptor:
                models.append(descriptor)

        return models

    def _parse_model_dir(self, model_dir: Path) -> ModelDescriptor:
        """Parse model directory into descriptor."""
        model_id = model_dir.name

        # Basic capability classification
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
            tags=["local", "huggingface"],
            metadata={"cache_path": str(model_dir)},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        """Classify capabilities from model ID."""
        model_lower = model_id.lower()

        profile = ModelCapabilityProfile()

        if any(name in model_lower for name in ["coder", "code"]):
            profile.coding = CapabilityLevel.CAPABLE

        profile.reasoning = CapabilityLevel.BASIC
        profile.instruction_following = CapabilityLevel.BASIC

        return profile
