from __future__ import annotations

import os

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor


class AnthropicDiscovery:
    """Discovers models from Anthropic."""

    def __init__(self):
        self.provider_id = "anthropic"
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.base_url = "https://api.anthropic.com"

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from Anthropic."""
        if not self.api_key:
            return []

        # Anthropic has a fixed set of models
        models = [
            self._create_model_descriptor("claude-3-opus-20240229", 200000, 0.015),
            self._create_model_descriptor("claude-3-sonnet-20240229", 200000, 0.003),
            self._create_model_descriptor("claude-3-haiku-20240307", 200000, 0.00025),
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
            profile.coding = CapabilityLevel.ADVANCED
            profile.reasoning = CapabilityLevel.EXPERT
            profile.planning = CapabilityLevel.EXPERT
            profile.summarization = CapabilityLevel.EXPERT
            profile.instruction_following = CapabilityLevel.EXPERT
            profile.tool_use = CapabilityLevel.EXPERT
            profile.long_context = CapabilityLevel.ADVANCED
        elif "sonnet" in model_id:
            profile.coding = CapabilityLevel.ADVANCED
            profile.reasoning = CapabilityLevel.ADVANCED
            profile.planning = CapabilityLevel.ADVANCED
            profile.summarization = CapabilityLevel.ADVANCED
            profile.instruction_following = CapabilityLevel.ADVANCED
            profile.tool_use = CapabilityLevel.ADVANCED
            profile.long_context = CapabilityLevel.ADVANCED
        elif "haiku" in model_id:
            profile.coding = CapabilityLevel.INTERMEDIATE
            profile.reasoning = CapabilityLevel.INTERMEDIATE
            profile.planning = CapabilityLevel.INTERMEDIATE
            profile.summarization = CapabilityLevel.INTERMEDIATE
            profile.instruction_following = CapabilityLevel.INTERMEDIATE
            profile.tool_use = CapabilityLevel.INTERMEDIATE
            profile.long_context = CapabilityLevel.INTERMEDIATE

        return profile
