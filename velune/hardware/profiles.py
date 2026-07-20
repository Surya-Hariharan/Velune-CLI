"""Adaptive runtime profiles derived from the detected hardware tier.

A RuntimeProfile is the bridge between *what the machine can do*
(HardwareProfile) and *how the runtime should behave* (context budgets,
retrieval depth, compression, council tier preference). It is computed once
at bootstrap and registered in the service container as ``runtime.profile``
so every layer can adapt without re-probing hardware.

Profiles never override an explicit user mode (/fast, /max); they only
shape the defaults of NORMAL mode and routing preferences.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from velune.hardware.detector import HardwareProfile, HardwareTier


class RuntimeProfileName(Enum):
    LOW_RESOURCE = "low_resource"
    BALANCED = "balanced"
    MAXIMUM = "maximum"


@dataclass(frozen=True)
class RuntimeProfile:
    """Hardware-adapted defaults for the session runtime."""

    name: RuntimeProfileName
    max_context_tokens: int  # NORMAL-mode context budget per call
    retrieval_depth: int  # memory + repo chunks pulled per turn
    context_compression: bool  # compress conversation before each call
    council_tier_ceiling: str  # highest auto-selected tier: "minimal" | "standard" | "full"
    prefer_local_models: bool  # bias routing toward local models
    max_local_model_b: float  # largest local model size (in B params) routing should pick
    description: str
    # Ceiling on simultaneous background work (embedding batches, key
    # re-verification): max(1, min(cpu_cores, per-tier ceiling)). Defaults to
    # today's uniform CAPABLE-tier value (4) so get_profile()'s static entries
    # — used directly by tests and cognition/orchestrator.py — are unaffected;
    # only derive_profile() (the live hardware-detection path) computes a
    # tier/core-count-specific value.
    background_concurrency: int = 4
    # Multiplier on background poll intervals (git-state polling, MCP watch,
    # proactive health checks): >1 = slower/less frequent. Same default-only-
    # via-derive_profile() rationale as background_concurrency above.
    background_poll_scale: float = 1.0

    @property
    def label(self) -> str:
        return self.name.value.replace("_", " ").upper()


_PROFILES: dict[RuntimeProfileName, RuntimeProfile] = {
    RuntimeProfileName.LOW_RESOURCE: RuntimeProfile(
        name=RuntimeProfileName.LOW_RESOURCE,
        max_context_tokens=4096,
        retrieval_depth=3,
        context_compression=True,
        council_tier_ceiling="minimal",
        prefer_local_models=False,
        max_local_model_b=3.5,
        description="Constrained hardware — compressed context, shallow retrieval, small models",
    ),
    RuntimeProfileName.BALANCED: RuntimeProfile(
        name=RuntimeProfileName.BALANCED,
        max_context_tokens=16384,
        retrieval_depth=8,
        context_compression=False,
        council_tier_ceiling="standard",
        prefer_local_models=True,
        max_local_model_b=8.0,
        description="Balanced — normal orchestration, 7B-class local models",
    ),
    RuntimeProfileName.MAXIMUM: RuntimeProfile(
        name=RuntimeProfileName.MAXIMUM,
        max_context_tokens=65536,
        retrieval_depth=16,
        context_compression=False,
        council_tier_ceiling="full",
        prefer_local_models=True,
        max_local_model_b=80.0,
        description="High-end hardware — deep retrieval, large context, full council available",
    ),
}

_TIER_TO_PROFILE: dict[HardwareTier, RuntimeProfileName] = {
    HardwareTier.CRITICAL: RuntimeProfileName.LOW_RESOURCE,
    HardwareTier.LOW: RuntimeProfileName.LOW_RESOURCE,
    HardwareTier.MARGINAL: RuntimeProfileName.BALANCED,
    HardwareTier.CAPABLE: RuntimeProfileName.BALANCED,
    HardwareTier.POWERFUL: RuntimeProfileName.MAXIMUM,
    HardwareTier.ELITE: RuntimeProfileName.MAXIMUM,
}

# background_concurrency / background_poll_scale vary by the full 6-value
# HardwareTier, independent of which 3-bucket RuntimeProfileName a machine
# maps to above — a CRITICAL and a LOW machine both land on LOW_RESOURCE for
# context/retrieval budgeting, but a CRITICAL machine's background work should
# still be throttled harder. Kept as separate tables rather than folded into
# _PROFILES so the static 3 profiles (used by get_profile()) stay simple,
# hardware-independent constants.
_TIER_CONCURRENCY_CEILING: dict[HardwareTier, int] = {
    HardwareTier.CRITICAL: 1,
    HardwareTier.LOW: 2,
    HardwareTier.MARGINAL: 3,
    HardwareTier.CAPABLE: 4,
    HardwareTier.POWERFUL: 6,
    HardwareTier.ELITE: 8,
}

_TIER_POLL_SCALE: dict[HardwareTier, float] = {
    HardwareTier.CRITICAL: 3.0,
    HardwareTier.LOW: 2.0,
    HardwareTier.MARGINAL: 1.25,
    HardwareTier.CAPABLE: 1.0,
    HardwareTier.POWERFUL: 0.85,
    HardwareTier.ELITE: 0.75,
}


def get_profile(name: RuntimeProfileName) -> RuntimeProfile:
    return _PROFILES[name]


def derive_profile(hardware: HardwareProfile) -> RuntimeProfile:
    """Map the detected hardware tier onto a runtime profile.

    Memory pressure demotes one level: a machine whose *available* RAM is under
    25% of total is treated one profile lower than its tier suggests, so a
    loaded workstation degrades gracefully instead of thrashing. This demotion
    only affects context/retrieval budgeting (``name`` and the fields that come
    from it) — background concurrency/poll-scale are CPU/IO-bound, not
    context-budget-bound, so they're derived straight from ``hardware.tier``
    and ``hardware.cpu_cores`` without going through the demoted name.
    """
    name = _TIER_TO_PROFILE.get(hardware.tier, RuntimeProfileName.BALANCED)

    if hardware.total_ram_gb > 0:
        available_ratio = hardware.available_ram_gb / hardware.total_ram_gb
        if available_ratio < 0.25:
            if name == RuntimeProfileName.MAXIMUM:
                name = RuntimeProfileName.BALANCED
            elif name == RuntimeProfileName.BALANCED:
                name = RuntimeProfileName.LOW_RESOURCE

    ceiling = _TIER_CONCURRENCY_CEILING.get(hardware.tier, 4)
    concurrency = max(1, min(hardware.cpu_cores, ceiling)) if hardware.cpu_cores else ceiling
    poll_scale = _TIER_POLL_SCALE.get(hardware.tier, 1.0)

    return replace(
        _PROFILES[name],
        background_concurrency=concurrency,
        background_poll_scale=poll_scale,
    )
