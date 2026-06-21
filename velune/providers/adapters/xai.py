"""xAI (Grok) provider adapter — OpenAI-compatible endpoint."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key


class XAIProvider(OpenAIProvider):
    """xAI Grok provider.  Wire-compatible with the OpenAI chat API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.x.ai/v1",
    ) -> None:
        super().__init__(api_key=api_key or get_key("xai"), base_url=base_url)

    @property
    def provider_id(self) -> str:
        return "xai"

    async def list_models(self) -> list[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id="grok-2",
                display_name="Grok 2",
                provider_id="xai",
                context_length=131072,
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
                tags=["cloud", "xai"],
            ),
            ModelDescriptor(
                model_id="grok-2-mini",
                display_name="Grok 2 Mini",
                provider_id="xai",
                context_length=131072,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.INTERMEDIATE,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.INTERMEDIATE,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                tags=["cloud", "xai", "mini"],
            ),
        ]
