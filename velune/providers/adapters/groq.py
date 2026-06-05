"""Groq provider adapter — OpenAI-compatible endpoint, free tier."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key


class GroqProvider(OpenAIProvider):
    """Groq Cloud provider.  Wire-compatible with the OpenAI chat API."""

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
        return [
            ModelDescriptor(
                model_id="llama-3.3-70b-versatile",
                display_name="Llama 3.3 70B Versatile",
                provider_id="groq",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.ADVANCED,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                speed_tier="fast",
                tags=["cloud", "groq", "free", "llama"],
                metadata={"free_tier": True},
            ),
            ModelDescriptor(
                model_id="mixtral-8x7b-32768",
                display_name="Mixtral 8x7B",
                provider_id="groq",
                context_length=32768,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.INTERMEDIATE,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.INTERMEDIATE,
                    "long_context": CapabilityLevel.INTERMEDIATE,
                },
                is_local=False,
                speed_tier="fast",
                tags=["cloud", "groq", "free", "mixtral"],
                metadata={"free_tier": True},
            ),
            ModelDescriptor(
                model_id="gemma2-9b-it",
                display_name="Gemma 2 9B IT",
                provider_id="groq",
                context_length=8192,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.INTERMEDIATE,
                    "planning": CapabilityLevel.BASIC,
                    "summarization": CapabilityLevel.INTERMEDIATE,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.BASIC,
                    "long_context": CapabilityLevel.BASIC,
                },
                is_local=False,
                speed_tier="fast",
                tags=["cloud", "groq", "free", "gemma"],
                metadata={"free_tier": True},
            ),
        ]
