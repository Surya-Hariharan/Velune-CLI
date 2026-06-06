from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key


class AnthropicDiscovery:
    """Discovers models from Anthropic."""

    def __init__(self):
        self.provider_id = "anthropic"
        self.api_key = get_key("anthropic")
        self.base_url = "https://api.anthropic.com"

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from Anthropic."""
        if not self.api_key:
            return []

        # Anthropic has a fixed set of models
        models = [
            self._create_model_descriptor("claude-opus-4-5", 200000, 0.015),
            self._create_model_descriptor("claude-sonnet-4-5", 200000, 0.003),
            self._create_model_descriptor("claude-haiku-4-5", 200000, 0.00025),
        ]

        return models

    def _create_model_descriptor(self, model_id: str, context_length: int, cost_per_1k: float) -> ModelDescriptor:
        """Create a model descriptor."""
        capabilities = self._classify_capabilities(model_id)

        return ModelDescriptor(
            model_id=model_id,
            provider_id=self.provider_id,
            display_name=model_id,
            context_length=context_length,
            capabilities=capabilities,
            quantization=None,
            vram_required_gb=None,
            parameter_count_b=None,
            speed_tier="medium",
            cost_per_1k_tokens=cost_per_1k,
            tags=["cloud", "anthropic"],
            metadata={},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        """Classify capabilities for Anthropic models."""
        profile = ModelCapabilityProfile()

        if "opus" in model_id:
            profile.coding = CapabilityLevel.EXPERT
            profile.reasoning = CapabilityLevel.EXPERT
            profile.planning = CapabilityLevel.EXPERT
            profile.summarization = CapabilityLevel.EXPERT
            profile.instruction_following = CapabilityLevel.EXPERT
            profile.tool_use = CapabilityLevel.EXPERT
            profile.long_context = CapabilityLevel.EXPERT
        elif "sonnet" in model_id:
            profile.coding = CapabilityLevel.ADVANCED
            profile.reasoning = CapabilityLevel.ADVANCED
            profile.planning = CapabilityLevel.ADVANCED
            profile.summarization = CapabilityLevel.ADVANCED
            profile.instruction_following = CapabilityLevel.ADVANCED
            profile.tool_use = CapabilityLevel.EXPERT
            profile.long_context = CapabilityLevel.ADVANCED
        elif "haiku" in model_id:
            profile.coding = CapabilityLevel.INTERMEDIATE
            profile.reasoning = CapabilityLevel.INTERMEDIATE
            profile.planning = CapabilityLevel.INTERMEDIATE
            profile.summarization = CapabilityLevel.INTERMEDIATE
            profile.instruction_following = CapabilityLevel.ADVANCED
            profile.tool_use = CapabilityLevel.ADVANCED
            profile.long_context = CapabilityLevel.INTERMEDIATE

        return profile
