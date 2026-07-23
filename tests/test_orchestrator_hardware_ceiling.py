"""CouncilOrchestrator must actually apply the hardware-derived council ceiling.

Before this fix, HardwareDetector/derive_profile correctly classified a weak
machine (e.g. an i3-class CPU with 8GB RAM) into a LOW_RESOURCE RuntimeProfile
with council_tier_ceiling="minimal" — but CouncilOrchestrator built its
TierClassifier purely from the static config default (max_council_tier="full"),
never consulting that profile. A detected-weak machine still auto-escalated to
the 6-agent FULL council. These tests pin down that the orchestrator now takes
the more restrictive of (config, hardware profile) — an explicit tighter config
value still wins, but a beefier machine's config default no longer silently
overrides what the hardware says is safe.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from velune.cognition.orchestrator import CouncilOrchestrator
from velune.hardware.profiles import RuntimeProfileName, get_profile
from velune.kernel.registry import ServiceContainer


def _make_orchestrator(monkeypatch, profile, *, config=None):
    container = ServiceContainer()
    if profile is not None:
        container.register_instance("runtime.profile", profile)
    monkeypatch.setattr("velune.kernel.registry.get_container", lambda: container)
    monkeypatch.setattr("velune.kernel.registry._container", container)

    return CouncilOrchestrator(
        provider_registry=MagicMock(),
        mapper=MagicMock(),
        config=config,
    )


def test_low_resource_profile_caps_default_full_ceiling_to_minimal(monkeypatch):
    profile = get_profile(RuntimeProfileName.LOW_RESOURCE)
    orch = _make_orchestrator(monkeypatch, profile)

    assert orch.tier_classifier.max_council_tier == "minimal"
    assert orch.tier_classifier.low_resource_mode is True


def test_maximum_profile_does_not_relax_an_explicit_stricter_config(monkeypatch):
    """A user who explicitly restricted the ceiling in config keeps that
    restriction even on a powerful machine — the hardware profile only ever
    pulls the ceiling down, never up."""
    from velune.kernel.config import CognitionConfig, ExecutionConfig, VeluneConfig

    config = MagicMock(spec=VeluneConfig)
    config.cognition = MagicMock(spec=CognitionConfig)
    config.cognition.max_council_tier = "standard"
    config.cognition.default_tier_override = "auto"
    config.execution = MagicMock(spec=ExecutionConfig)
    config.execution.low_resource_mode = False

    profile = get_profile(RuntimeProfileName.MAXIMUM)  # ceiling="full"
    orch = _make_orchestrator(monkeypatch, profile, config=config)

    assert orch.tier_classifier.max_council_tier == "standard"


def test_maximum_profile_leaves_default_full_ceiling_untouched(monkeypatch):
    profile = get_profile(RuntimeProfileName.MAXIMUM)  # ceiling="full"
    orch = _make_orchestrator(monkeypatch, profile)

    assert orch.tier_classifier.max_council_tier == "full"
    assert orch.tier_classifier.low_resource_mode is False


def test_no_profile_registered_falls_back_to_config_only(monkeypatch):
    """If runtime.profile was never registered (e.g. a standalone/test
    construction), the orchestrator must not crash and must keep the old
    config-only behavior."""
    orch = _make_orchestrator(monkeypatch, profile=None)

    assert orch.tier_classifier.max_council_tier == "full"
    assert orch.tier_classifier.low_resource_mode is False


@pytest.mark.parametrize(
    "profile_name,expected_ceiling",
    [
        (RuntimeProfileName.LOW_RESOURCE, "minimal"),
        (RuntimeProfileName.BALANCED, "standard"),
        (RuntimeProfileName.MAXIMUM, "full"),
    ],
)
def test_each_profile_caps_the_default_config_correctly(
    monkeypatch, profile_name, expected_ceiling
):
    profile = get_profile(profile_name)
    orch = _make_orchestrator(monkeypatch, profile)
    assert orch.tier_classifier.max_council_tier == expected_ceiling
