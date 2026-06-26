"""Tests for per-role council sampling profiles."""

from __future__ import annotations

from velune.cognition.council.sampling import (
    coder_sample_count,
    get_sampling_profile,
)
from velune.models.specializations import CouncilRole


def test_every_role_has_a_profile() -> None:
    for role in CouncilRole:
        profile = get_sampling_profile(role)
        assert 0.0 <= profile.temperature <= 2.0
        assert 0.0 <= profile.top_p <= 1.0


def test_coder_is_exploratory_multisample_judges_are_greedy() -> None:
    coder = get_sampling_profile(CouncilRole.CODER)
    reviewer = get_sampling_profile(CouncilRole.REVIEWER)
    assert coder.n_samples >= 3
    assert reviewer.n_samples == 1
    # Judges sample colder than the coder so their scores are reproducible.
    assert reviewer.temperature < coder.temperature


def test_diverge_temperatures_are_distinct_and_padded() -> None:
    coder = get_sampling_profile(CouncilRole.CODER)
    temps = coder.sample_temperatures(3)
    assert len(temps) == 3
    assert len(set(temps)) > 1  # genuinely divergent
    # Requesting more samples than the schedule reuses the last (hottest) temp.
    padded = coder.sample_temperatures(5)
    assert len(padded) == 5
    assert padded[-1] == temps[-1]


def test_coder_sample_count_respects_constraints() -> None:
    # Low-resource collapses to a single fast sample.
    assert coder_sample_count(low_resource=True, degraded_diversity=False) == 1
    assert coder_sample_count(low_resource=True, degraded_diversity=True) == 1
    # Degraded diversity (one model for all roles) leans harder on sampling.
    assert coder_sample_count(low_resource=False, degraded_diversity=True) >= 3
    # Normal path uses the profile default.
    assert coder_sample_count(low_resource=False, degraded_diversity=False) >= 1
