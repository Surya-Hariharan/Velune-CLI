"""Model profile caching layer for empirical benchmark results."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Optional


class ModelProfileCache:
    """Persists probe results to avoid re-probing on every invocation."""

    CACHE_TTL_HOURS = 168  # Re-probe weekly (7 days * 24 hours = 168 hours)

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, model_id: str, provider_id: str) -> Optional[dict]:
        """Retrieve cached probe results if present and fresh."""
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text())
            key = f"{provider_id}/{model_id}"
            entry = data.get(key)
            if not entry:
                return None

            age_hours = (time.time() - entry["probed_at"]) / 3600.0
            if age_hours > self.CACHE_TTL_HOURS:
                return None  # Stale
            return entry
        except Exception:
            return None

    def set(self, model_id: str, provider_id: str, probe_results: dict) -> None:
        """Cache probe results locally, converting dataclass results to dictionaries."""
        import dataclasses

        data = {}
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text())
            except Exception:
                pass

        serialized_probes = {}
        for cap, result in probe_results.items():
            if dataclasses.is_dataclass(result):
                serialized_probes[cap] = dataclasses.asdict(result)
            elif isinstance(result, dict):
                serialized_probes[cap] = result
            else:
                # Fallback mapping
                serialized_probes[cap] = {
                    "capability": cap,
                    "score": getattr(result, "score", 0.0),
                    "latency_ms": getattr(result, "latency_ms", -1.0),
                    "passed": getattr(result, "passed", False),
                    "details": getattr(result, "details", ""),
                }

        key = f"{provider_id}/{model_id}"
        data[key] = {
            "probed_at": time.time(),
            "probes": serialized_probes,
        }
        self.cache_path.write_text(json.dumps(data, indent=2))
