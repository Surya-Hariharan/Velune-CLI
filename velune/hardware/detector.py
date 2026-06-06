"""Hardware capability detection — classifies the local machine into tiers
that map directly to which local LLM sizes can run comfortably."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HardwareTier(Enum):
    CRITICAL = "critical"   # < 8 GB RAM, no GPU — cannot run any local LLM
    LOW      = "low"        # 8 GB RAM, integrated GPU — 3B models only
    MARGINAL = "marginal"   # 16 GB RAM, integrated GPU — 3B fast / 7B slow
    CAPABLE  = "capable"    # 16 GB RAM + 6-8 GB VRAM — 7B comfortably
    POWERFUL = "powerful"   # 32 GB RAM + 12+ GB VRAM — 13B comfortably
    ELITE    = "elite"      # 64 GB+ or Apple Silicon 36 GB+ — 70B capable


@dataclass
class HardwareProfile:
    total_ram_gb: float
    available_ram_gb: float
    gpu_name: str | None
    vram_total_gb: float | None
    is_apple_silicon: bool
    cpu_cores: int
    platform: str                   # "linux" | "darwin" | "windows"
    tier: HardwareTier
    recommended_model_size: str     # "3B" | "7B" | "13B" | "30B" | "70B" | "none"
    can_run_local_llm: bool
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class HardwareDetector:
    def detect(self) -> HardwareProfile:
        import platform

        import psutil

        total_ram = psutil.virtual_memory().total / (1024 ** 3)
        available_ram = psutil.virtual_memory().available / (1024 ** 3)
        cpu_cores = psutil.cpu_count(logical=False) or 1
        sys_platform = platform.system().lower()
        is_apple_silicon = (
            sys_platform == "darwin"
            and platform.processor() == "arm"
        )

        gpu_name, vram_gb = self._detect_gpu()
        tier, rec_model, can_run = self._classify(total_ram, vram_gb, is_apple_silicon)
        warnings, suggestions = self._build_advice(total_ram, vram_gb, is_apple_silicon, tier)

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

    def _detect_gpu(self) -> tuple[str | None, float | None]:
        # NVIDIA via pynvml
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_gb = mem.total / (1024 ** 3)
            return name, round(vram_gb, 1)
        except Exception:
            pass

        # Apple Silicon — unified memory: all RAM is usable as VRAM
        import platform
        if platform.system() == "Darwin" and platform.processor() == "arm":
            import psutil
            total_ram = psutil.virtual_memory().total / (1024 ** 3)
            return "Apple Silicon (unified memory)", round(total_ram, 1)

        # AMD via ROCm
        try:
            import subprocess
            result = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True, text=True, timeout=5,
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

        if is_apple and ram_gb >= 36:
            return HardwareTier.ELITE, "70B", True
        if is_apple and ram_gb >= 16:
            return HardwareTier.POWERFUL, "13B", True
        if ram_gb >= 64 and effective_vram >= 24:
            return HardwareTier.ELITE, "70B", True
        if ram_gb >= 32 and effective_vram >= 12:
            return HardwareTier.POWERFUL, "13B", True
        if ram_gb >= 16 and effective_vram >= 6:
            return HardwareTier.CAPABLE, "7B", True
        if ram_gb >= 16:
            return HardwareTier.MARGINAL, "3B", True
        if ram_gb >= 8:
            return HardwareTier.LOW, "3B", True
        return HardwareTier.CRITICAL, "none", False

    def _build_advice(
        self,
        ram_gb: float,
        vram_gb: float | None,
        is_apple: bool,
        tier: HardwareTier,
    ) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        suggestions: list[str] = []

        if tier == HardwareTier.CRITICAL:
            warnings.append(
                f"Only {ram_gb:.0f} GB RAM detected — cannot run any local LLM"
            )
            suggestions.append("Configure a cloud provider (Groq free tier recommended)")
            suggestions.append("Run: velune init --provider groq")

        elif tier == HardwareTier.LOW:
            warnings.append(
                f"{ram_gb:.0f} GB RAM with no discrete GPU — "
                "local inference will be very slow (3–8 tok/s)"
            )
            suggestions.append("Use Groq free tier for fast cloud inference")
            suggestions.append("For local: stick to 3B models (phi3-mini, gemma2:2b)")

        elif tier == HardwareTier.MARGINAL:
            warnings.append(
                f"{ram_gb:.0f} GB RAM, integrated GPU only — "
                "7B models will run at CPU speed (~5–10 tok/s)"
            )
            suggestions.append("Use Groq for council tasks, local 3B for quick queries")

        return warnings, suggestions
