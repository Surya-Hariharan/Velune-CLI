"""Groq provider adapter — OpenAI-compatible endpoint, free tier."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.provider import ProviderHealth
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key, has_key

GROQ_MODELS: list[ModelDescriptor] = [
    ModelDescriptor(
        model_id="llama-3.3-70b-versatile",
        provider_id="groq",
        display_name="Llama 3.3 70B Versatile",
        context_length=131072,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.EXPERT,
            instruction_following=CapabilityLevel.EXPERT,
            tool_use=CapabilityLevel.ADVANCED,
            long_context=CapabilityLevel.EXPERT,
        ),
        tags=["cloud", "groq", "free", "llama"],
        metadata={"free_tier": True},
    ),
    ModelDescriptor(
        model_id="llama-3.1-8b-instant",
        provider_id="groq",
        display_name="Llama 3.1 8B Instant",
        context_length=131072,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.INTERMEDIATE,
            reasoning=CapabilityLevel.INTERMEDIATE,
            planning=CapabilityLevel.INTERMEDIATE,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.INTERMEDIATE,
            long_context=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "groq", "free", "llama", "instant"],
        metadata={"free_tier": True},
    ),
    ModelDescriptor(
        model_id="mixtral-8x7b-32768",
        provider_id="groq",
        display_name="Mixtral 8x7B",
        context_length=32768,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.INTERMEDIATE,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.INTERMEDIATE,
            long_context=CapabilityLevel.INTERMEDIATE,
        ),
        tags=["cloud", "groq", "free", "mixtral"],
        metadata={"free_tier": True},
    ),
    ModelDescriptor(
        model_id="gemma2-9b-it",
        provider_id="groq",
        display_name="Gemma 2 9B Instruct",
        context_length=8192,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.INTERMEDIATE,
            reasoning=CapabilityLevel.INTERMEDIATE,
            planning=CapabilityLevel.BASIC,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.BASIC,
            long_context=CapabilityLevel.BASIC,
        ),
        tags=["cloud", "groq", "free", "gemma"],
        metadata={"free_tier": True},
    ),
    ModelDescriptor(
        model_id="llama-3.2-11b-vision-preview",
        provider_id="groq",
        display_name="Llama 3.2 11B Vision",
        context_length=8192,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.INTERMEDIATE,
            reasoning=CapabilityLevel.INTERMEDIATE,
            planning=CapabilityLevel.BASIC,
            summarization=CapabilityLevel.INTERMEDIATE,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.BASIC,
            long_context=CapabilityLevel.BASIC,
        ),
        tags=["cloud", "groq", "free", "llama", "vision"],
        metadata={"free_tier": True},
    ),
]


class GroqProvider(OpenAIProvider):
    """Groq Cloud provider — wire-compatible with the OpenAI chat API.

    Uses Groq's custom LPU hardware for extremely fast free-tier inference.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.groq.com/openai/v1",
    ) -> None:
        super().__init__(api_key=api_key or get_key("groq"), base_url=base_url)

    @property
    def provider_id(self) -> str:
        return "groq"

    async def list_models(self) -> list[ModelDescriptor]:
        return GROQ_MODELS

    async def health_check(self) -> ProviderHealth:
        if not has_key("groq"):
            return ProviderHealth.UNAVAILABLE
        return await super().health_check()

    def get_provider_info(self) -> dict:
        return {
            "provider_id": "groq",
            "display_name": "Groq",
            "is_free_tier": True,
            "base_url": "https://api.groq.com/openai/v1",
            "note": "Free tier — extremely fast inference via custom LPU hardware",
        }
