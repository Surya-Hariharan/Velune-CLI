"""GPU/VRAM detection."""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

# Process-wide cache: GPU topology does not change during a run, and the probe
# (subprocess + driver query) is comparatively expensive. Memoizing also dedupes
# the historically-double probe (runtime bootstrap + hardware detector).
_GPU_CACHE: dict[str, Any] | None = None

# Subprocess probes must never block startup if a GPU driver is wedged.
_PROBE_TIMEOUT = 3.0

# Disk cache. ``nvidia-smi`` can take ~2s to spawn on a laptop, and a process
# memo only helps within a single run — every cold CLI launch paid that cost.
# GPU topology is effectively static between reboots, so we persist the result
# and only re-probe when the cache is missing or older than the TTL. VRAM *free*
# is the only volatile field; callers that need a live free-VRAM reading should
# probe directly rather than rely on this startup cache.
_DISK_CACHE_PATH = Path.home() / ".velune" / "hardware.json"
_DISK_CACHE_TTL = 24 * 60 * 60  # 24h


def _read_disk_cache() -> dict[str, Any] | None:
    try:
        raw = json.loads(_DISK_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    ts = raw.get("_cached_at")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > _DISK_CACHE_TTL:
        return None
    info = raw.get("info")
    return info if isinstance(info, dict) else None


def _write_disk_cache(info: dict[str, Any]) -> None:
    try:
        _DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DISK_CACHE_PATH.write_text(
            json.dumps({"_cached_at": time.time(), "info": info}),
            encoding="utf-8",
        )
    except Exception:
        pass


class GPUDetector:
    """Detects GPU capabilities and VRAM."""

    def detect(self, *, use_cache: bool = True) -> dict[str, Any]:
        """Detect GPU information.

        Memoized for the process lifetime and, when *use_cache* is set, backed
        by a short-lived on-disk cache so repeated cold launches skip the slow
        ``nvidia-smi`` subprocess. Pass ``use_cache=False`` to force a fresh
        probe (e.g. for an accurate free-VRAM reading).
        """
        global _GPU_CACHE
        if use_cache and _GPU_CACHE is not None:
            return dict(_GPU_CACHE)

        if use_cache:
            cached = _read_disk_cache()
            if cached is not None:
                _GPU_CACHE = dict(cached)
                return dict(cached)

        info = {
            "has_gpu": False,
            "gpu_type": None,
            "vram_total_gb": None,
            "vram_free_gb": None,
            "cuda_available": False,
        }

        # Try NVIDIA -> AMD (ROCm) -> Apple Silicon (Metal), first hit wins.
        for probe in (self._detect_nvidia, self._detect_amd, self._detect_metal):
            result = probe()
            if result:
                info.update(result)
                break

        _GPU_CACHE = dict(info)
        if use_cache:
            _write_disk_cache(info)
        return info

    def _detect_nvidia(self) -> dict[str, Any] | None:
        """Detect NVIDIA GPU via nvidia-smi."""
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.free",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=_PROBE_TIMEOUT,
            )

            lines = result.stdout.strip().split("\n")
            if not lines:
                return None

            parts = lines[0].split(",")
            gpu_name = parts[0].strip()
            memory_total = parts[1].strip().replace(" MiB", "")
            memory_free = parts[2].strip().replace(" MiB", "")

            return {
                "has_gpu": True,
                "gpu_type": "nvidia",
                "gpu_name": gpu_name,
                "vram_total_gb": float(memory_total) / 1024,
                "vram_free_gb": float(memory_free) / 1024,
                "cuda_available": True,
            }
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def _detect_amd(self) -> dict[str, Any] | None:
        """Detect AMD GPU via rocm-smi."""
        try:
            subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True,
                text=True,
                check=True,
                timeout=_PROBE_TIMEOUT,
            )

            # Parse rocm-smi output (simplified)
            return {
                "has_gpu": True,
                "gpu_type": "amd",
                "cuda_available": False,
            }
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def _detect_metal(self) -> dict[str, Any] | None:
        """Detect Apple Silicon GPU via Metal."""
        try:
            import platform

            if platform.machine() != "arm64":
                return None

            # Apple Silicon has unified memory
            import psutil

            total_memory = psutil.virtual_memory().total / (1024**3)  # GB

            return {
                "has_gpu": True,
                "gpu_type": "apple_silicon",
                "vram_total_gb": total_memory,
                "vram_free_gb": total_memory * 0.8,  # Assume 80% available
                "cuda_available": False,
            }
        except Exception:
            return None
