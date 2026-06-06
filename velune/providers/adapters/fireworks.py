"""Fireworks.AI provider adapter — OpenAI-compatible REST API."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key

FIREWORKS_MODELS: list[ModelDescriptor] = [
    ModelDescriptor(
        model_id="accounts/fireworks/models/llama-v3p3-70b-instruct",
        display_name="Llama 3.3 70B Instruct",
        provider_id="fireworks",
        context_length=131072,
        capabilities={
            "coding": CapabilityLevel.ADVANCED,
            "reasoning": CapabilityLevel.ADVANCED,
            "planning": CapabilityLevel.ADVANCED,
            "summarization": CapabilityLevel.EXPERT,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.ADVANCED,
        },
        speed_tier="fast",
        is_local=False,
        cost_per_1k_tokens=0.0009,
        tags=["cloud", "fireworks", "llama"],
    ),
    ModelDescriptor(
        model_id="accounts/fireworks/models/deepseek-r1",
        display_name="DeepSeek R1",
        provider_id="fireworks",
        context_length=163840,
        capabilities={
            "coding": CapabilityLevel.EXPERT,
            "reasoning": CapabilityLevel.EXPERT,
            "planning": CapabilityLevel.EXPERT,
            "summarization": CapabilityLevel.ADVANCED,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.ADVANCED,
        },
        speed_tier="slow",
        is_local=False,
        cost_per_1k_tokens=0.003,
        tags=["cloud", "fireworks", "deepseek", "reasoning"],
    ),
    ModelDescriptor(
        model_id="accounts/fireworks/models/qwen2p5-coder-32b-instruct",
        display_name="Qwen 2.5 Coder 32B Instruct",
        provider_id="fireworks",
        context_length=131072,
        capabilities={
            "coding": CapabilityLevel.EXPERT,
            "reasoning": CapabilityLevel.ADVANCED,
            "planning": CapabilityLevel.ADVANCED,
            "summarization": CapabilityLevel.ADVANCED,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.ADVANCED,
        },
        speed_tier="medium",
        is_local=False,
        cost_per_1k_tokens=0.0009,
        tags=["cloud", "fireworks", "qwen", "coding"],
    ),
    ModelDescriptor(
        model_id="accounts/fireworks/models/mixtral-8x22b-instruct",
        display_name="Mixtral 8x22B Instruct",
        provider_id="fireworks",
        context_length=65536,
        capabilities={
            "coding": CapabilityLevel.ADVANCED,
            "reasoning": CapabilityLevel.ADVANCED,
            "planning": CapabilityLevel.ADVANCED,
            "summarization": CapabilityLevel.EXPERT,
            "instruction_following": CapabilityLevel.ADVANCED,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.ADVANCED,
        },
        speed_tier="medium",
        is_local=False,
        cost_per_1k_tokens=0.0009,
        tags=["cloud", "fireworks", "mixtral", "moe"],
    ),
]


class FireworksProvider(OpenAIProvider):
    """Fireworks.AI — fast, cheap open-model inference."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.fireworks.ai/inference/v1",
    ) -> None:
        self._api_key = api_key or get_key("fireworks")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=False,
            max_context_window=131072,
        )

    @property
    def provider_id(self) -> str:
        return "fireworks"

    async def list_models(self) -> list[ModelDescriptor]:
        return list(FIREWORKS_MODELS)
