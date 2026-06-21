"""Fireworks.AI model discovery."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key


class FireworksDiscovery:
    """Returns the hardcoded Fireworks.AI model list when a key is configured."""

    provider_id = "fireworks"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("fireworks"):
            return []

        return [
            ModelDescriptor(
                model_id="accounts/fireworks/models/llama-v3p3-70b-instruct",
                display_name="Llama 3.3 70B Instruct",
                provider_id="fireworks",
                context_length=131072,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.ADVANCED,
                    reasoning=CapabilityLevel.ADVANCED,
                    planning=CapabilityLevel.ADVANCED,
                    summarization=CapabilityLevel.EXPERT,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.0009,
                tags=["cloud", "fireworks", "llama"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="accounts/fireworks/models/deepseek-r1",
                display_name="DeepSeek R1",
                provider_id="fireworks",
                context_length=163840,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.EXPERT,
                    reasoning=CapabilityLevel.EXPERT,
                    planning=CapabilityLevel.EXPERT,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="slow",
                cost_per_1k_tokens=0.003,
                tags=["cloud", "fireworks", "deepseek", "reasoning"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="accounts/fireworks/models/qwen2p5-coder-32b-instruct",
                display_name="Qwen 2.5 Coder 32B Instruct",
                provider_id="fireworks",
                context_length=131072,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.EXPERT,
                    reasoning=CapabilityLevel.ADVANCED,
                    planning=CapabilityLevel.ADVANCED,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.EXPERT,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="medium",
                cost_per_1k_tokens=0.0009,
                tags=["cloud", "fireworks", "qwen", "coding"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="accounts/fireworks/models/mixtral-8x22b-instruct",
                display_name="Mixtral 8x22B Instruct",
                provider_id="fireworks",
                context_length=65536,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.ADVANCED,
                    reasoning=CapabilityLevel.ADVANCED,
                    planning=CapabilityLevel.ADVANCED,
                    summarization=CapabilityLevel.EXPERT,
                    instruction_following=CapabilityLevel.ADVANCED,
                    tool_use=CapabilityLevel.ADVANCED,
                    long_context=CapabilityLevel.ADVANCED,
                ),
                speed_tier="medium",
                cost_per_1k_tokens=0.0009,
                tags=["cloud", "fireworks", "mixtral", "moe"],
                metadata={},
            ),
        ]
