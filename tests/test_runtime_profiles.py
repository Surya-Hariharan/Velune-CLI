"""Tests for hardware-derived runtime profiles and hardware-aware selection."""

from __future__ import annotations

from velune.cli.model_selector import fits_hardware
from velune.cli.modes import MODE_CONFIGS, ModeManager, SessionMode
from velune.core.types.model import ModelDescriptor
from velune.hardware.detector import HardwareProfile, HardwareTier
from velune.hardware.profiles import (
    RuntimeProfileName,
    derive_profile,
    get_profile,
)


def _hardware(
    tier: HardwareTier,
    total_ram: float = 16.0,
    available_ram: float = 8.0,
) -> HardwareProfile:
    return HardwareProfile(
        total_ram_gb=total_ram,
        available_ram_gb=available_ram,
        gpu_name=None,
        vram_total_gb=None,
        is_apple_silicon=False,
        cpu_cores=8,
        platform="windows",
        tier=tier,
        recommended_model_size="7B",
        can_run_local_llm=True,
    )


def _model(model_id: str = "m", is_local: bool = True, params_b: float | None = None):
    return ModelDescriptor(
        model_id=model_id,
        provider_id="ollama" if is_local else "openai",
        display_name=model_id,
        context_length=8192,
        capabilities=None,
        is_local=is_local,
        parameter_count_b=params_b,
    )


class TestDeriveProfile:
    def test_critical_tier_maps_to_low_resource(self):
        profile = derive_profile(_hardware(HardwareTier.CRITICAL, 4, 2))
        assert profile.name == RuntimeProfileName.LOW_RESOURCE
        assert profile.context_compression is True

    def test_capable_tier_maps_to_balanced(self):
        profile = derive_profile(_hardware(HardwareTier.CAPABLE))
        assert profile.name == RuntimeProfileName.BALANCED

    def test_elite_tier_maps_to_maximum(self):
        profile = derive_profile(_hardware(HardwareTier.ELITE, 64, 48))
        assert profile.name == RuntimeProfileName.MAXIMUM
        assert profile.council_tier_ceiling == "full"

    def test_memory_pressure_demotes_one_level(self):
        # ELITE tier but only 10% RAM available → demoted to BALANCED
        profile = derive_profile(_hardware(HardwareTier.ELITE, 64, 6.4))
        assert profile.name == RuntimeProfileName.BALANCED

        profile = derive_profile(_hardware(HardwareTier.CAPABLE, 16, 1.6))
        assert profile.name == RuntimeProfileName.LOW_RESOURCE

    def test_label_is_human_readable(self):
        assert get_profile(RuntimeProfileName.LOW_RESOURCE).label == "LOW RESOURCE"


class TestModeManagerProfileOverlay:
    def test_normal_mode_adopts_profile_budgets(self):
        profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
        manager = ModeManager(runtime_profile=profile)
        config = manager.config
        assert config.max_context_tokens == profile.max_context_tokens
        assert config.retrieval_depth == profile.retrieval_depth
        assert config.context_compression is True

    def test_explicit_modes_ignore_profile(self):
        profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
        manager = ModeManager(runtime_profile=profile)
        manager.set_mode(SessionMode.GODLY)
        assert manager.config == MODE_CONFIGS[SessionMode.GODLY]

    def test_no_profile_keeps_static_normal_config(self):
        manager = ModeManager()
        assert manager.config == MODE_CONFIGS[SessionMode.NORMAL]


class TestFitsHardware:
    def test_cloud_models_always_fit(self):
        profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
        assert fits_hardware(_model(is_local=False, params_b=405.0), profile)

    def test_oversized_local_model_rejected(self):
        profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
        assert not fits_hardware(_model(params_b=70.0), profile)

    def test_small_local_model_fits(self):
        profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
        assert fits_hardware(_model(params_b=3.0), profile)

    def test_unknown_size_assumed_to_fit(self):
        profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
        assert fits_hardware(_model(params_b=None), profile)

    def test_no_profile_means_everything_fits(self):
        assert fits_hardware(_model(params_b=70.0), None)
