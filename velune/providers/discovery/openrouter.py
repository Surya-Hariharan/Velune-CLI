"""OpenRouter model discovery with 1-hour local cache."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.keystore import get_key

_CACHE_TTL = 3600.0  # seconds


def _cache_path() -> Path:
    """Resolve the cache file location, preferring the project .velune dir."""
    project = Path.cwd() / ".velune"
    if project.exists():
        return project / "openrouter_models_cache.json"
    home = Path.home() / ".velune"
    home.mkdir(parents=True, exist_ok=True)
    return home / "openrouter_models_cache.json"


class OpenRouterDiscovery:
    """Fetches the full OpenRouter model catalogue with a 1-hour disk cache."""

    provider_id = "openrouter"

    async def discover(self) -> list[ModelDescriptor]:
        if not get_key("openrouter"):
            return []

        cached = self._load_cache()
        if cached is not None:
            return cached

        models = await self._fetch()
        self._save_cache(models)
        return models

    def _load_cache(self) -> list[ModelDescriptor] | None:
        path = _cache_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("cached_at", 0) > _CACHE_TTL:
                return None
            return [self._raw_to_descriptor(m) for m in data.get("models", [])]
        except Exception:
            return None

    def _save_cache(self, models: list[ModelDescriptor]) -> None:
        path = _cache_path()
        try:
            raw = [
                {
                    "id": m.model_id,
                    "name": m.display_name,
                    "context_length": m.context_length,
                    "cost_per_1k_tokens": m.cost_per_1k_tokens,
                }
                for m in models
            ]
            path.write_text(
                json.dumps({"cached_at": time.time(), "models": raw}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _fetch(self) -> list[ModelDescriptor]:
        api_key = get_key("openrouter")
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "Velune CLI",
                "X-Title": "Velune CLI",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                return [self._raw_to_descriptor(m) for m in data.get("data", [])]
        except Exception:
            return []

    def _raw_to_descriptor(self, raw: dict) -> ModelDescriptor:
        model_id = raw.get("id", "unknown")
        context = raw.get("context_length") or 4096
        pricing = raw.get("pricing", {})
        cost = float(pricing.get("prompt", 0) or 0) * 1000
        return ModelDescriptor(
            model_id=model_id,
            provider_id="openrouter",
            display_name=raw.get("name") or model_id,
            context_length=context,
            capabilities=ModelCapabilityProfile(
                coding=CapabilityLevel.INTERMEDIATE,
                reasoning=CapabilityLevel.INTERMEDIATE,
                instruction_following=CapabilityLevel.ADVANCED,
            ),
            speed_tier="medium",
            cost_per_1k_tokens=cost if cost > 0 else None,
            tags=["cloud", "openrouter"],
            metadata={},
        )
