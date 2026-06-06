"""Together.AI provider adapter — OpenAI-compatible REST API."""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key

TOGETHER_MODELS: list[ModelDescriptor] = [
    ModelDescriptor(
        model_id="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        display_name="Llama 3.3 70B Instruct Turbo",
        provider_id="together",
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
        cost_per_1k_tokens=0.00088,
        tags=["cloud", "together", "llama", "turbo"],
    ),
    ModelDescriptor(
        model_id="meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo",
        display_name="Llama 3.2 11B Vision Instruct",
        provider_id="together",
        context_length=131072,
        capabilities={
            "coding": CapabilityLevel.INTERMEDIATE,
            "reasoning": CapabilityLevel.INTERMEDIATE,
            "planning": CapabilityLevel.INTERMEDIATE,
            "summarization": CapabilityLevel.ADVANCED,
            "instruction_following": CapabilityLevel.ADVANCED,
            "tool_use": CapabilityLevel.INTERMEDIATE,
            "long_context": CapabilityLevel.INTERMEDIATE,
        },
        speed_tier="fast",
        is_local=False,
        cost_per_1k_tokens=0.00018,
        tags=["cloud", "together", "llama", "vision", "cheap"],
    ),
    ModelDescriptor(
        model_id="Qwen/Qwen2.5-Coder-32B-Instruct",
        display_name="Qwen 2.5 Coder 32B Instruct",
        provider_id="together",
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
        cost_per_1k_tokens=0.0008,
        tags=["cloud", "together", "qwen", "coding"],
    ),
    ModelDescriptor(
        model_id="deepseek-ai/DeepSeek-R1",
        display_name="DeepSeek R1",
        provider_id="together",
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
        tags=["cloud", "together", "deepseek", "reasoning"],
    ),
    ModelDescriptor(
        model_id="mistralai/Mistral-7B-Instruct-v0.3",
        display_name="Mistral 7B Instruct v0.3",
        provider_id="together",
        context_length=32768,
        capabilities={
            "coding": CapabilityLevel.INTERMEDIATE,
            "reasoning": CapabilityLevel.INTERMEDIATE,
            "planning": CapabilityLevel.INTERMEDIATE,
            "summarization": CapabilityLevel.ADVANCED,
            "instruction_following": CapabilityLevel.ADVANCED,
            "tool_use": CapabilityLevel.INTERMEDIATE,
            "long_context": CapabilityLevel.BASIC,
        },
        speed_tier="fast",
        is_local=False,
        cost_per_1k_tokens=0.0002,
        tags=["cloud", "together", "mistral", "cheap"],
    ),
]


class TogetherProvider(OpenAIProvider):
    """Together.AI — 50+ open models via OpenAI-compatible inference."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.together.xyz/v1",
    ) -> None:
        self._api_key = api_key or get_key("together")
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
        return "together"

    async def list_models(self) -> list[ModelDescriptor]:
        return list(TOGETHER_MODELS)
