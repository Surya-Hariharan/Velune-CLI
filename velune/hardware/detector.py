"""Hardware capability detection — classifies the local machine into tiers
that map directly to which local LLM sizes can run comfortably."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HardwareTier(Enum):
    CRITICAL = "critical"  # < 8 GB RAM, no GPU — cannot run any local LLM
    LOW = "low"  # 8 GB RAM, integrated GPU — 3B models only
    MARGINAL = "marginal"  # 16 GB RAM, integrated GPU — 3B fast / 7B slow
    CAPABLE = "capable"  # 16 GB RAM + 6-8 GB VRAM — 7B comfortably
    POWERFUL = "powerful"  # 32 GB RAM + 12+ GB VRAM — 13B comfortably
    ELITE = "elite"  # 64 GB+ or Apple Silicon 36 GB+ — 70B capable


@dataclass
class HardwareProfile:
    total_ram_gb: float
    available_ram_gb: float
    gpu_name: str | None
    vram_total_gb: float | None
    is_apple_silicon: bool
    cpu_cores: int
    platform: str  # "linux" | "darwin" | "windows"
    tier: HardwareTier
    recommended_model_size: str  # "3B" | "7B" | "13B" | "30B" | "70B" | "none"
    can_run_local_llm: bool
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


_PROFILE_CACHE: HardwareProfile | None = None

# Common nominal RAM capacities (GB). Usable RAM always reports a little under
# the nominal figure, so we snap a measured value up to the nearest capacity
# when it falls within tolerance.
_NOMINAL_RAM_STEPS = (4, 8, 16, 24, 32, 48, 64, 96, 128, 192, 256)
_RAM_TOLERANCE_GB = 0.8


def _nominal_ram_gb(measured_gb: float) -> float:
    """Snap *measured_gb* up to its nominal capacity (e.g. 15.8 -> 16)."""
    for step in _NOMINAL_RAM_STEPS:
        if measured_gb >= step - _RAM_TOLERANCE_GB and measured_gb < step:
            return float(step)
    return measured_gb


class HardwareDetector:
    def detect(self) -> HardwareProfile:
        global _PROFILE_CACHE
        # Memoized for the process lifetime — RAM/GPU topology is stable during
        # a run and the GPU probe is comparatively expensive.
        if _PROFILE_CACHE is not None:
            return _PROFILE_CACHE

        import platform

        import psutil

        total_ram = psutil.virtual_memory().total / (1024**3)
        available_ram = psutil.virtual_memory().available / (1024**3)
        cpu_cores = psutil.cpu_count(logical=False) or 1
        sys_platform = platform.system().lower()
        is_apple_silicon = sys_platform == "darwin" and platform.processor() == "arm"

        gpu_name, vram_gb = self._detect_gpu()
        tier, rec_model, can_run = self._classify(total_ram, vram_gb, is_apple_silicon)
        warnings, suggestions = self._build_advice(
            total_ram, vram_gb, gpu_name, is_apple_silicon, tier
        )

        _PROFILE_CACHE = HardwareProfile(
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
        return _PROFILE_CACHE

    def _detect_gpu(self) -> tuple[str | None, float | None]:
        # Reuse the runtime's already-completed GPU probe if present. The CLI
        # bootstrap runs GPUDetector before HardwareDetector; without this the
        # GPU is probed twice (~0.4-0.6s of duplicated subprocess/driver work).
        try:
            from velune.providers.discovery import gpu as _gpu_mod

            cache = _gpu_mod._GPU_CACHE
            if cache is not None:
                # Runtime already completed a GPU probe — trust it rather than
                # re-running pynvml/subprocess for the same hardware.
                if not cache.get("has_gpu"):
                    return None, None
                name = cache.get("gpu_name") or cache.get("gpu_type")
                vram = cache.get("vram_total_gb")
                if name and vram:
                    return str(name), round(float(vram), 1)
        except Exception:
            pass

        # NVIDIA via pynvml
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_gb = mem.total / (1024**3)
            return name, round(vram_gb, 1)
        except Exception:
            pass

        # Apple Silicon — unified memory: all RAM is usable as VRAM
        import platform

        if platform.system() == "Darwin" and platform.processor() == "arm":
            import psutil

            total_ram = psutil.virtual_memory().total / (1024**3)
            return "Apple Silicon (unified memory)", round(total_ram, 1)

        # AMD via ROCm
        try:
            import subprocess

            result = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Total" in line:
                        mb = int(line.split()[-1])
                        return "AMD GPU (ROCm)", round(mb / 1024, 1)
        except Exception:
            pass

        return None, None

    def _classify(
        self,
        ram_gb: float,
        vram_gb: float | None,
        is_apple: bool,
    ) -> tuple[HardwareTier, str, bool]:
        effective_vram = vram_gb or 0.0

        # Physical RAM never reports as its nominal capacity — a "16 GB" machine
        # shows ~15.6-15.9 GB usable once firmware/iGPU reservations are taken
        # out. Comparing against the round number (>= 16) silently demotes every
        # 16 GB box to the LOW tier, which then prints a "no discrete GPU"
        # warning even on a machine with a dedicated GPU. Bucket to the nearest
        # nominal capacity with a ~0.7 GB tolerance before classifying.
        nominal_ram = _nominal_ram_gb(ram_gb)

        if is_apple and nominal_ram >= 36:
            return HardwareTier.ELITE, "70B", True
        if is_apple and nominal_ram >= 16:
            return HardwareTier.POWERFUL, "13B", True
        if nominal_ram >= 64 and effective_vram >= 24:
            return HardwareTier.ELITE, "70B", True
        if nominal_ram >= 32 and effective_vram >= 12:
            return HardwareTier.POWERFUL, "13B", True
        if nominal_ram >= 16 and effective_vram >= 6:
            return HardwareTier.CAPABLE, "7B", True
        if nominal_ram >= 16:
            return HardwareTier.MARGINAL, "3B", True
        if nominal_ram >= 8:
            return HardwareTier.LOW, "3B", True
        return HardwareTier.CRITICAL, "none", False

    def _build_advice(
        self,
        ram_gb: float,
        vram_gb: float | None,
        gpu_name: str | None,
        is_apple: bool,
        tier: HardwareTier,
    ) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        suggestions: list[str] = []
        # Describe the *actual* accelerator rather than assuming one based on the
        # tier — a 16 GB laptop with a real dGPU must not be told it has none.
        has_dgpu = bool(vram_gb) and not is_apple
        nominal = int(round(_nominal_ram_gb(ram_gb)))

        if tier == HardwareTier.CRITICAL:
            warnings.append(f"Only {nominal} GB RAM detected — cannot run any local LLM")
            suggestions.append("Configure a cloud provider (Groq free tier recommended)")
            suggestions.append("Run: velune init --provider groq")

        elif tier == HardwareTier.LOW:
            gpu_phrase = f"{gpu_name} ({vram_gb:.0f} GB)" if has_dgpu else "no discrete GPU"
            warnings.append(
                f"{nominal} GB RAM with {gpu_phrase} — "
                "local inference will be slow; prefer small models"
            )
            suggestions.append("Use Groq free tier for fast cloud inference")
            suggestions.append("For local: stick to 3B models (phi3-mini, gemma2:2b)")

        elif tier == HardwareTier.MARGINAL:
            warnings.append(
                f"{nominal} GB RAM, integrated GPU only — "
                "7B models will run at CPU speed (~5–10 tok/s)"
            )
            suggestions.append("Use Groq for council tasks, local 3B for quick queries")

        return warnings, suggestions
