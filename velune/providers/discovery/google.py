"""Google Gemini model discovery."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key


class GoogleDiscovery:
    """Returns the hardcoded Gemini model list when a key is configured."""

    provider_id = "google"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("google"):
            return []

        return [
            ModelDescriptor(
                model_id="gemini-2.0-flash",
                provider_id="google",
                display_name="Gemini 2.0 Flash",
                context_length=1048576,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.ADVANCED,
                    reasoning=CapabilityLevel.ADVANCED,
                    planning=CapabilityLevel.ADVANCED,
                    summarization=CapabilityLevel.EXPERT,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.EXPERT,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.00015,
                tags=["cloud", "google", "flash"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="gemini-1.5-pro",
                provider_id="google",
                display_name="Gemini 1.5 Pro",
                context_length=2097152,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.ADVANCED,
                    reasoning=CapabilityLevel.EXPERT,
                    planning=CapabilityLevel.EXPERT,
                    summarization=CapabilityLevel.EXPERT,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.EXPERT,
                    long_context=CapabilityLevel.EXPERT,
                ),
                speed_tier="medium",
                cost_per_1k_tokens=0.00125,
                tags=["cloud", "google", "pro"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="gemini-1.5-flash",
                provider_id="google",
                display_name="Gemini 1.5 Flash",
                context_length=1048576,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.INTERMEDIATE,
                    reasoning=CapabilityLevel.INTERMEDIATE,
                    planning=CapabilityLevel.INTERMEDIATE,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.ADVANCED,
                    tool_use=CapabilityLevel.INTERMEDIATE,
                    long_context=CapabilityLevel.EXPERT,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.000075,
                tags=["cloud", "google", "flash"],
                metadata={},
            ),
        ]
