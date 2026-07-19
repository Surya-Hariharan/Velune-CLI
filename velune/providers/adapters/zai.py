"""Z.ai (Zhipu) provider adapter — OpenAI-compatible endpoint, GLM model family."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key, has_key

ZAI_MODELS: list[ModelDescriptor] = [
    ModelDescriptor(
        model_id="glm-4.6",
        provider_id="zai",
        display_name="GLM-4.6",
        context_length=200000,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.ADVANCED,
            long_context=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "zai", "glm"],
        metadata={},
    ),
    ModelDescriptor(
        model_id="glm-4.5",
        provider_id="zai",
        display_name="GLM-4.5",
        context_length=128000,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.INTERMEDIATE,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.ADVANCED,
            long_context=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "zai", "glm"],
        metadata={},
    ),
    ModelDescriptor(
        model_id="glm-4.5-air",
        provider_id="zai",
        display_name="GLM-4.5 Air",
        context_length=128000,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.INTERMEDIATE,
            reasoning=CapabilityLevel.INTERMEDIATE,
            planning=CapabilityLevel.BASIC,
            summarization=CapabilityLevel.INTERMEDIATE,
            instruction_following=CapabilityLevel.INTERMEDIATE,
            tool_use=CapabilityLevel.INTERMEDIATE,
            long_context=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "zai", "glm", "free"],
        metadata={"free_tier": True},
    ),
]


class ZaiProvider(OpenAIProvider):
    """Z.ai (Zhipu) GLM model family. Wire-compatible with the OpenAI chat API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.z.ai/api/paas/v4",
    ) -> None:
        super().__init__(api_key=api_key or get_key("zai"), base_url=base_url)

    @property
    def provider_id(self) -> str:
        return "zai"

    async def list_models(self) -> list[ModelDescriptor]:
        return ZAI_MODELS

    async def health_check(self):
        from velune.core.types.provider import ProviderHealth

        if not has_key("zai"):
            return ProviderHealth.UNAVAILABLE
        return await super().health_check()

    def get_provider_info(self) -> dict:
        return {
            "provider_id": "zai",
            "display_name": "Z.ai",
            "is_free_tier": False,
            "base_url": "https://api.z.ai/api/paas/v4",
            "note": "GLM model family — GLM-4.5 Air is free tier",
        }
