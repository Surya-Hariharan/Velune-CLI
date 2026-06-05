"""Groq model discovery — free-tier models."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key


class GroqDiscovery:
    """Returns the hardcoded Groq model list when a key is configured."""

    provider_id = "groq"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("groq"):
            return []

        return [
            ModelDescriptor(
                model_id="llama-3.3-70b-versatile",
                provider_id="groq",
                display_name="Llama 3.3 70B Versatile",
                context_length=128000,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.ADVANCED,
                    reasoning=CapabilityLevel.ADVANCED,
                    planning=CapabilityLevel.ADVANCED,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.0,
                tags=["cloud", "groq", "free", "llama"],
                metadata={"free_tier": True},
            ),
            ModelDescriptor(
                model_id="mixtral-8x7b-32768",
                provider_id="groq",
                display_name="Mixtral 8x7B",
                context_length=32768,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.INTERMEDIATE,
                    reasoning=CapabilityLevel.INTERMEDIATE,
                    planning=CapabilityLevel.INTERMEDIATE,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.ADVANCED,
                    tool_use=CapabilityLevel.INTERMEDIATE,
                    long_context=CapabilityLevel.INTERMEDIATE,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.0,
                tags=["cloud", "groq", "free", "mixtral"],
                metadata={"free_tier": True},
            ),
            ModelDescriptor(
                model_id="gemma2-9b-it",
                provider_id="groq",
                display_name="Gemma 2 9B IT",
                context_length=8192,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.INTERMEDIATE,
                    reasoning=CapabilityLevel.INTERMEDIATE,
                    planning=CapabilityLevel.BASIC,
                    summarization=CapabilityLevel.INTERMEDIATE,
                    instruction_following=CapabilityLevel.ADVANCED,
                    tool_use=CapabilityLevel.BASIC,
                    long_context=CapabilityLevel.BASIC,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.0,
                tags=["cloud", "groq", "free", "gemma"],
                metadata={"free_tier": True},
            ),
        ]
