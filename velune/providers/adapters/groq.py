"""Groq provider adapter — OpenAI-compatible endpoint, free tier."""

from __future__ import annotations

import logging

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.provider import ProviderHealth
from velune.providers.adapters.openai import OpenAIProvider
from velune.providers.keystore import get_key, has_key

logger = logging.getLogger(__name__)

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
        model_id="openai/gpt-oss-120b",
        provider_id="groq",
        display_name="GPT-OSS 120B",
        context_length=131072,
        is_local=False,
        free_tier=True,
        cost_per_1k_tokens=0.0,
        speed_tier="fast",
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.ADVANCED,
            planning=CapabilityLevel.ADVANCED,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=CapabilityLevel.ADVANCED,
            long_context=CapabilityLevel.ADVANCED,
        ),
        tags=["cloud", "groq", "free", "gpt-oss"],
        metadata={"free_tier": True},
    ),
]
# Note on this list: `mixtral-8x7b-32768`, `gemma2-9b-it`, and
# `llama-3.2-11b-vision-preview` were removed 2026-07 — Groq decommissioned
# all three (confirmed via a live GET /v1/models call; requests to them now
# 400). A stale entry here isn't cosmetic: the Council role-mapper scores
# roles against this static list and will happily assign a role to a model
# that no longer exists, crashing that agent's turn. list_models() is static
# rather than a live query because the curated CapabilityProfile scores
# below aren't available from Groq's API — but that means this list needs a
# periodic manual check against a real `GET /v1/models` call.
#
# `qwen/qwen3-32b` was removed 2026-08-12 — Groq deprecated it 2026-07-17
# (confirmed via console.groq.com/docs/deprecations), recommending
# `openai/gpt-oss-120b` (already listed above) as the successor. Selecting it
# produced a live HTTP 404 from /openai/v1/chat/completions, surfaced to the
# user as a generic InferenceError — see also the 404 error classification in
# adapters/openai.py._raise_provider_error and RETRYABLE_EXCEPTIONS in
# providers/retrying.py, which previously retried this 404 three times before
# giving up, since it wasn't distinguished from a transient failure.

# Live /models entries that are not chat-completion models; never offered to
# the Council (audio, safety classifiers, TTS).
_NON_CHAT_MARKERS = ("whisper", "orpheus", "prompt-guard", "safeguard", "guard", "tts")


async def fetch_live_model_ids(
    api_key: str | None, base_url: str, timeout: float = 5.0
) -> set[str] | None:
    """Return the model IDs Groq currently serves to *api_key*, or None on any failure."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
            resp.raise_for_status()
            return {m["id"] for m in resp.json().get("data", []) if "id" in m}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.debug("Could not fetch live Groq model list (using static catalog): %s", exc)
        return None


def reconcile_with_live(live_ids: set[str] | None) -> list[ModelDescriptor]:
    """Intersect the curated catalog with what Groq actually serves.

    The static list carries curated capability scores but rots whenever Groq
    deprecates a model (a stale entry becomes a live HTTP 404 mid-turn). With a
    live listing, drop curated entries Groq no longer serves and add any new
    chat model with a conservative default profile. Without one (offline, bad
    key), fall back to the static catalog unchanged.
    """
    if not live_ids:
        return list(GROQ_MODELS)
    curated = {m.model_id: m for m in GROQ_MODELS}
    result = [m for mid, m in curated.items() if mid in live_ids]
    for mid in sorted(live_ids - curated.keys()):
        if any(marker in mid.lower() for marker in _NON_CHAT_MARKERS):
            continue
        result.append(
            ModelDescriptor(
                model_id=mid,
                provider_id="groq",
                display_name=mid.split("/")[-1],
                context_length=131072,
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
                    long_context=CapabilityLevel.ADVANCED,
                ),
                tags=["cloud", "groq", "free", "live-discovered"],
                metadata={"free_tier": True},
            )
        )
    return result or list(GROQ_MODELS)


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
        live = await fetch_live_model_ids(get_key("groq"), self._base_url)
        return reconcile_with_live(live)

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
