"""Reconcile a provider's curated model catalog with what its API actually serves.

Hardcoded catalogs rot: providers retire models, and a stale entry becomes a
live HTTP 404 in the middle of a Council turn (this bit Groq twice). A live
``/models`` listing is the source of truth for *which* models exist and, where
the API reports it, their context window; the curated entries add what the API
doesn't provide (capability scores, cost, speed tier).

Offline, with a bad key, or on any HTTP error, callers get the curated catalog
unchanged — discovery never gets worse than the static list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger(__name__)

LiveModels = dict[str, "int | None"]
"""``{model_id: context_window_or_None}`` as reported by a provider."""


async def get_json(
    url: str, headers: dict[str, str], *, params: dict[str, Any] | None = None, timeout: float = 8.0
) -> Any | None:
    """GET *url* and return decoded JSON, or None on any failure (logged at debug)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Live model listing failed for %s (using curated catalog): %s", url, exc)
        return None


def default_descriptor(
    model_id: str,
    provider_id: str,
    window: int,
    *,
    display_name: str | None = None,
    strong: bool = False,
    cost_per_1k: float = 0.0,
    free_tier: bool = False,
) -> ModelDescriptor:
    """A conservative descriptor for a model the curated catalog doesn't know yet."""
    top = CapabilityLevel.ADVANCED if strong else CapabilityLevel.INTERMEDIATE
    return ModelDescriptor(
        model_id=model_id,
        provider_id=provider_id,
        display_name=display_name or model_id,
        context_length=window,
        is_local=False,
        free_tier=free_tier,
        cost_per_1k_tokens=cost_per_1k,
        speed_tier="medium",
        capabilities=ModelCapabilityProfile(
            coding=top,
            reasoning=top,
            planning=top,
            summarization=CapabilityLevel.ADVANCED,
            instruction_following=CapabilityLevel.ADVANCED,
            tool_use=top,
            long_context=CapabilityLevel.ADVANCED if window >= 32768 else CapabilityLevel.BASIC,
        ),
        tags=["cloud", provider_id, "live-discovered"],
        metadata={},
    )


def reconcile(
    curated: list[ModelDescriptor],
    live: LiveModels | None,
    provider_id: str,
    *,
    accept: Callable[[str], bool] = lambda _mid: True,
    fallback_window: Callable[[str], int] = lambda _mid: 8192,
    is_strong: Callable[[str], bool] = lambda _mid: False,
) -> list[ModelDescriptor]:
    """Intersect *curated* with *live*, then append new accepted live models.

    ``accept`` filters out non-chat models (embeddings, audio, image, ...);
    ``fallback_window`` supplies a context length when the API doesn't report
    one; ``is_strong`` picks the capability tier for models we have no profile for.
    """
    if not live:
        return list(curated)
    curated_ids = {m.model_id for m in curated}
    result = [m for m in curated if m.model_id in live]
    for mid in sorted(live.keys() - curated_ids):
        if not accept(mid):
            continue
        window = live[mid] or fallback_window(mid)
        result.append(default_descriptor(mid, provider_id, window, strong=is_strong(mid)))
    return result or list(curated)
