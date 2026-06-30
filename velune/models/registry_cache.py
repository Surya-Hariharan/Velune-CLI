"""Persistent JSON cache for the model registry.

Serialises discovered ``ModelDescriptor`` objects to ``~/.velune/model_registry_cache.json``
so that ``velune models list`` is populated without running a fresh scan.

TTL policy
----------
- Local providers (Ollama, LM Studio, GGUF, Docker, …): 5 minutes
  (daemon or drive contents can change quickly)
- Cloud providers (OpenAI, Anthropic, …): 60 minutes

The cache is loaded automatically by ``ModelCapabilityRegistry.__init__``.
After each successful scan, ``ModelCapabilityRegistry.refresh`` calls ``save()``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger("velune.models.registry_cache")

DEFAULT_CACHE_PATH = Path.home() / ".velune" / "model_registry_cache.json"

_LOCAL_TTL_S: float = 300.0   # 5 min
_CLOUD_TTL_S: float = 3600.0  # 60 min

# provider_id prefixes that identify local endpoints
_LOCAL_PROVIDER_PREFIXES = (
    "ollama", "lmstudio", "gguf", "llamacpp", "docker",
    "openai-compat", "vllm", "tgi", "localai", "nvidia_nim_local",
)


def _is_local_provider(provider_id: str) -> bool:
    return any(provider_id.startswith(p) for p in _LOCAL_PROVIDER_PREFIXES)


def _ttl_for(provider_id: str) -> float:
    env_override = os.environ.get("VELUNE_REGISTRY_CACHE_TTL_HOURS")
    if env_override:
        try:
            return float(env_override) * 3600.0
        except ValueError:
            pass
    return _LOCAL_TTL_S if _is_local_provider(provider_id) else _CLOUD_TTL_S


class ModelRegistryCache:
    """Read/write the model registry cache file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_CACHE_PATH

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, models: list) -> None:
        """Atomically persist *models* (list[ModelDescriptor]) to the cache file."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": time.time(),
                "models": [self._serialize(m) for m in models],
            }
            data = json.dumps(payload, indent=2)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(str(tmp), str(self.path))
            _log.debug("Registry cache saved: %d models → %s", len(models), self.path)
        except Exception as exc:
            _log.warning("Could not save registry cache: %s", exc)

    def load(self) -> list:
        """Return valid (non-stale) ``ModelDescriptor`` objects from the cache.

        Returns an empty list when the cache is absent, corrupt, or fully stale.
        """
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("Could not read registry cache: %s", exc)
            return []

        saved_at: float = payload.get("saved_at", 0.0)
        now = time.time()
        raw_models: list[dict] = payload.get("models", [])

        valid: list = []
        for raw in raw_models:
            provider_id = raw.get("provider_id", "")
            ttl = _ttl_for(provider_id)
            if (now - saved_at) <= ttl:
                try:
                    valid.append(self._deserialize(raw))
                except Exception as exc:
                    _log.debug("Could not deserialize cached model %s: %s", raw.get("model_id"), exc)

        _log.debug("Registry cache loaded: %d/%d fresh models", len(valid), len(raw_models))
        return valid

    def is_fresh(self) -> bool:
        """True if any entry in the cache is within its TTL."""
        if not self.path.exists():
            return False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            saved_at = float(payload.get("saved_at", 0.0))
            models = payload.get("models", [])
            if not models:
                return False
            # Consider fresh if the oldest TTL allows it (use minimum TTL for safety)
            return (time.time() - saved_at) <= _LOCAL_TTL_S
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize(model) -> dict:
        """Convert a ModelDescriptor to a JSON-serializable dict."""
        try:
            d = model.model_dump()
            # Coerce enum/IntEnum values to int for JSON
            caps = d.get("capabilities") or {}
            if isinstance(caps, dict):
                d["capabilities"] = {
                    k: int(v) if hasattr(v, "__int__") else v
                    for k, v in caps.items()
                }
            return d
        except Exception:
            # Fallback: manual extraction of known fields
            return {
                "model_id": getattr(model, "model_id", ""),
                "provider_id": getattr(model, "provider_id", ""),
                "display_name": getattr(model, "display_name", ""),
                "context_length": getattr(model, "context_length", 4096),
                "speed_tier": getattr(model, "speed_tier", "medium"),
                "is_local": getattr(model, "is_local", False),
                "location": getattr(model, "location", None),
                "health": getattr(model, "health", "unknown"),
                "last_latency_ms": getattr(model, "last_latency_ms", None),
                "tags": list(getattr(model, "tags", [])),
                "metadata": dict(getattr(model, "metadata", {})),
            }

    @staticmethod
    def _deserialize(raw: dict):
        """Reconstruct a ModelDescriptor from a raw dict."""
        from velune.core.types.model import (
            CapabilityLevel,
            ModelCapabilityProfile,
            ModelDescriptor,
        )

        caps_raw = raw.pop("capabilities", None) or {}
        if isinstance(caps_raw, dict):
            profile_kwargs = {
                k: CapabilityLevel(int(v)) if isinstance(v, (int, float)) else v
                for k, v in caps_raw.items()
                if k in ModelCapabilityProfile.model_fields
            }
            capabilities = ModelCapabilityProfile(**profile_kwargs)
        else:
            capabilities = ModelCapabilityProfile()

        raw["capabilities"] = capabilities
        return ModelDescriptor(**raw)
