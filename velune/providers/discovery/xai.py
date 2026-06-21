"""xAI (Grok) model discovery."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key


class XAIDiscovery:
    """Returns the hardcoded xAI Grok model list when a key is configured."""

    provider_id = "xai"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("xai"):
            return []

        return [
            ModelDescriptor(
                model_id="grok-2",
                provider_id="xai",
                display_name="Grok 2",
                context_length=131072,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.ADVANCED,
                    reasoning=CapabilityLevel.ADVANCED,
                    planning=CapabilityLevel.ADVANCED,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="medium",
                cost_per_1k_tokens=0.005,
                tags=["cloud", "xai"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="grok-2-mini",
                provider_id="xai",
                display_name="Grok 2 Mini",
                context_length=131072,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.INTERMEDIATE,
                    reasoning=CapabilityLevel.INTERMEDIATE,
                    planning=CapabilityLevel.INTERMEDIATE,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.ADVANCED,
                    tool_use=CapabilityLevel.INTERMEDIATE,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.0005,
                tags=["cloud", "xai", "mini"],
                metadata={},
            ),
        ]
