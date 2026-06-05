"""OpenRouter provider adapter — OpenAI-compatible with dynamic model listing."""

from __future__ import annotations

import httpx

from velune.core.errors.provider import ProviderAuthenticationError
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key

_REFERER_HEADERS = {
    "HTTP-Referer": "Velune CLI",
    "X-Title": "Velune CLI",
}


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter provider — routes to many upstream models via the OpenAI API shape."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        super().__init__(api_key=api_key or get_key("openrouter"), base_url=base_url)

    @property
    def provider_id(self) -> str:
        return "openrouter"

    async def initialize(self) -> None:
        """Override to inject the OpenRouter-required headers."""
        if not self._api_key:
            raise ProviderAuthenticationError(
                "OpenRouter API key not found — set OPENROUTER_API_KEY or run: velune config set-key openrouter"
            )
        if not self.client:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                **_REFERER_HEADERS,
            }
            self.client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=300.0,
            )

    async def list_models(self) -> list[ModelDescriptor]:
        """Fetch the current model catalogue from the OpenRouter API."""
        await self.initialize()
        assert self.client is not None
        try:
            resp = await self.client.get("/models")
            resp.raise_for_status()
            data = resp.json()
            return [self._parse_model(m) for m in data.get("data", [])]
        except Exception:
            return []

    def _parse_model(self, raw: dict) -> ModelDescriptor:
        model_id = raw.get("id", "unknown")
        context = raw.get("context_length") or 4096
        pricing = raw.get("pricing", {})
        cost_prompt = float(pricing.get("prompt", 0) or 0) * 1000
        profile = ModelCapabilityProfile(
            coding=CapabilityLevel.INTERMEDIATE,
            reasoning=CapabilityLevel.INTERMEDIATE,
            instruction_following=CapabilityLevel.ADVANCED,
        )
        return ModelDescriptor(
            model_id=model_id,
            display_name=raw.get("name") or model_id,
            provider_id="openrouter",
            context_length=context,
            capabilities=profile,
            is_local=False,
            cost_per_1k_tokens=cost_prompt if cost_prompt > 0 else None,
            tags=["cloud", "openrouter"],
            metadata={"raw": raw},
        )
