"""Model profile caching layer for empirical benchmark results."""

from __future__ import annotations

import json
import time
from pathlib import Path


class ModelProfileCache:
    """Persists probe results to avoid re-probing on every invocation."""

    CACHE_TTL_HOURS = 168  # Re-probe weekly (7 days * 24 hours = 168 hours)

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, model_id: str, provider_id: str) -> dict | None:
        """Retrieve cached probe results if present and fresh."""
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
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
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
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
        import os
        import tempfile

        # Atomically write to temp file, then rename/replace
        temp_dir = self.cache_path.parent
        with tempfile.NamedTemporaryFile(
            "w", dir=temp_dir, delete=False, encoding="utf-8"
        ) as temp_file:
            json.dump(data, temp_file, indent=2)
            temp_file_name = temp_file.name
        try:
            os.replace(temp_file_name, str(self.cache_path))
        except Exception:
            if os.path.exists(temp_file_name):
                os.remove(temp_file_name)
            raise
