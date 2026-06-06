"""Together.AI model discovery."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key


class TogetherDiscovery:
    """Returns the hardcoded Together.AI model list when a key is configured."""

    provider_id = "together"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("together"):
            return []

        return [
            ModelDescriptor(
                model_id="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                display_name="Llama 3.3 70B Instruct Turbo",
                provider_id="together",
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
                cost_per_1k_tokens=0.00088,
                tags=["cloud", "together", "llama", "turbo"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo",
                display_name="Llama 3.2 11B Vision Instruct",
                provider_id="together",
                context_length=131072,
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
                cost_per_1k_tokens=0.00018,
                tags=["cloud", "together", "llama", "vision", "cheap"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="Qwen/Qwen2.5-Coder-32B-Instruct",
                display_name="Qwen 2.5 Coder 32B Instruct",
                provider_id="together",
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
                cost_per_1k_tokens=0.0008,
                tags=["cloud", "together", "qwen", "coding"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="deepseek-ai/DeepSeek-R1",
                display_name="DeepSeek R1",
                provider_id="together",
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
                tags=["cloud", "together", "deepseek", "reasoning"],
                metadata={},
            ),
            ModelDescriptor(
                model_id="mistralai/Mistral-7B-Instruct-v0.3",
                display_name="Mistral 7B Instruct v0.3",
                provider_id="together",
                context_length=32768,
                capabilities=ModelCapabilityProfile(
                    coding=CapabilityLevel.INTERMEDIATE,
                    reasoning=CapabilityLevel.INTERMEDIATE,
                    planning=CapabilityLevel.INTERMEDIATE,
                    summarization=CapabilityLevel.ADVANCED,
                    instruction_following=CapabilityLevel.ADVANCED,
                    tool_use=CapabilityLevel.INTERMEDIATE,
                    long_context=CapabilityLevel.BASIC,
                ),
                speed_tier="fast",
                cost_per_1k_tokens=0.0002,
                tags=["cloud", "together", "mistral", "cheap"],
                metadata={},
            ),
        ]
