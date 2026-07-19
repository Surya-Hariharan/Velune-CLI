"""Meta Llama API provider adapter — OpenAI-compatible endpoint.

Meta's official first-party Llama API (as opposed to Llama weights served
through a third party like Groq/Together/Fireworks). Wire-compatible with the
OpenAI chat API via its ``/compat/v1`` endpoint.
"""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key, has_key

META_MODELS: list[ModelDescriptor] = [
    ModelDescriptor(
        model_id="Llama-4-Maverick-17B-128E-Instruct-FP8",
        provider_id="meta",
        display_name="Llama 4 Maverick",
        context_length=1048576,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.ADVANCED,
            long_context=CapabilityLevel.EXPERT,
            vision=CapabilityLevel.ADVANCED,
            multimodal=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "meta", "llama"],
        metadata={},
    ),
    ModelDescriptor(
        model_id="Llama-4-Scout-17B-16E-Instruct-FP8",
        provider_id="meta",
        display_name="Llama 4 Scout",
        context_length=10485760,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.INTERMEDIATE,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.INTERMEDIATE,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.INTERMEDIATE,
            long_context=CapabilityLevel.EXPERT,
            vision=CapabilityLevel.ADVANCED,
            multimodal=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "meta", "llama"],
        metadata={},
    ),
    ModelDescriptor(
        model_id="Llama-3.3-70B-Instruct",
        provider_id="meta",
        display_name="Llama 3.3 70B",
        context_length=131072,
        is_local=False,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.INTERMEDIATE,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.INTERMEDIATE,
            long_context=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "meta", "llama"],
        metadata={},
    ),
]


class MetaProvider(OpenAIProvider):
    """Meta's first-party Llama API. Wire-compatible with the OpenAI chat API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.llama.com/compat/v1",
    ) -> None:
        super().__init__(api_key=api_key or get_key("meta"), base_url=base_url)

    @property
    def provider_id(self) -> str:
        return "meta"

    async def list_models(self) -> list[ModelDescriptor]:
        return META_MODELS

    async def health_check(self):
        from velune.core.types.provider import ProviderHealth

        if not has_key("meta"):
            return ProviderHealth.UNAVAILABLE
        return await super().health_check()

    def get_provider_info(self) -> dict:
        return {
            "provider_id": "meta",
            "display_name": "Meta",
            "is_free_tier": True,
            "base_url": "https://api.llama.com/compat/v1",
            "note": "Meta's official first-party Llama API",
        }
