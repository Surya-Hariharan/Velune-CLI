"""GPU/VRAM detection."""

import subprocess
from typing import Any

# Process-wide cache: GPU topology does not change during a run, and the probe
# (subprocess + driver query) is comparatively expensive. Memoizing also dedupes
# the historically-double probe (runtime bootstrap + hardware detector).
_GPU_CACHE: dict[str, Any] | None = None

# Subprocess probes must never block startup if a GPU driver is wedged.
_PROBE_TIMEOUT = 3.0


class GPUDetector:
    """Detects GPU capabilities and VRAM."""

    def detect(self) -> dict[str, Any]:
        """Detect GPU information (memoized for the process lifetime)."""
        global _GPU_CACHE
        if _GPU_CACHE is not None:
            return dict(_GPU_CACHE)
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
