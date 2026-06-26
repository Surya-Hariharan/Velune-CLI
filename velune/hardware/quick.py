"""Fast, GPU-free hardware profile for the synchronous startup path.

The full :class:`~velune.hardware.detector.HardwareDetector` runs a GPU probe
(pynvml / subprocess) that costs ~0.5s cold — far too much to pay before the
first prompt appears. :func:`quick_hardware_profile` derives a usable profile
from RAM/CPU only (both effectively free via ``psutil``) so the REPL can build
its mode budgets and status bar immediately. The accurate profile, including
the GPU, is computed in the background warm-up and hot-swapped in.

This deliberately does **not** populate ``HardwareDetector``'s process-wide
cache, so the later full detection still runs and produces the authoritative
profile.
"""

from __future__ import annotations

from velune.hardware.detector import HardwareDetector, HardwareProfile


def quick_hardware_profile() -> HardwareProfile:
    """Return a provisional profile from RAM/CPU only (no GPU probe).

    GPU is reported as absent here; classification therefore biases toward the
    conservative (CPU-only) tier for the brief window before the background
    probe upgrades it. Never raises — falls back to a safe minimal profile.
    """
    detector = HardwareDetector()
    try:
        import platform

        import psutil

        total_ram = psutil.virtual_memory().total / (1024**3)
        available_ram = psutil.virtual_memory().available / (1024**3)
        cpu_cores = psutil.cpu_count(logical=False) or 1
        sys_platform = platform.system().lower()
        is_apple_silicon = sys_platform == "darwin" and platform.processor() == "arm"
    except Exception:
        total_ram = available_ram = 8.0
        cpu_cores = 1
        sys_platform = "unknown"
        is_apple_silicon = False

    # Apple Silicon's unified memory is usable as VRAM, so even the provisional
    # profile should reflect that rather than assuming a CPU-only machine.
    vram_gb = round(total_ram, 1) if is_apple_silicon else None
    gpu_name = "Apple Silicon (unified memory)" if is_apple_silicon else None

    tier, rec_model, can_run = detector._classify(total_ram, vram_gb, is_apple_silicon)
    warnings, suggestions = detector._build_advice(
        total_ram, vram_gb, gpu_name, is_apple_silicon, tier
    )

    return HardwareProfile(
        total_ram_gb=round(total_ram, 1),
        available_ram_gb=round(available_ram, 1),
        gpu_name=gpu_name,
        vram_total_gb=vram_gb,
        is_apple_silicon=is_apple_silicon,
        cpu_cores=cpu_cores,
        platform=sys_platform,
        tier=tier,
        recommended_model_size=rec_model,
        can_run_local_llm=can_run,
        warnings=warnings,
        suggestions=suggestions,
    )
