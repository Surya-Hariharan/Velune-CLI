"""Per-role sampling profiles for the Reasoning Council.

Centralizes the temperature / top_p / token-budget / sample-count knobs that
were previously scattered as magic literals across the individual agent call
sites (``coder.py`` temp=0.3, ``reviewer.py`` temp=0.1, ...).  Giving each
council role an explicit, named sampling profile is what makes the agents
*genuinely* differentiated rather than identical models wearing different hats.

On a single local GPU every role often resolves to the same physical model, so
solution diversity has to come from sampling instead of from distinct backends:
the Coder runs several independent samples at staggered temperatures
(self-consistency), while critics stay near-deterministic so their judgments are
stable and comparable across candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

from velune.models.specializations import CouncilRole


@dataclass(frozen=True)
class RoleSamplingProfile:
    """Sampling configuration for a single council role.

    Attributes:
        temperature: Base sampling temperature for the role.
        top_p: Nucleus sampling cutoff.
        max_tokens: Optional hard output-token budget (``None`` = provider default).
        n_samples: Number of independent samples to draw for this role. Only the
            Coder uses ``> 1`` (multi-solver diverge round); judges stay at 1.
        diverge_temperatures: When ``n_samples > 1``, the explicit per-sample
            temperatures used to force divergence between candidates. If shorter
            than ``n_samples`` the last value is reused.
    """

    temperature: float
    top_p: float = 1.0
    max_tokens: int | None = None
    n_samples: int = 1
    diverge_temperatures: tuple[float, ...] = ()

    def sample_temperatures(self, n: int | None = None) -> list[float]:
        """Return ``n`` temperatures to use for independent samples.

        Falls back to ``self.temperature`` when no diverge schedule is defined.
        """
        count = n if n is not None else self.n_samples
        count = max(1, count)
        if not self.diverge_temperatures:
            return [self.temperature] * count
        schedule = list(self.diverge_temperatures)
        if len(schedule) >= count:
            return schedule[:count]
        # Pad by repeating the final (most exploratory) temperature.
        return schedule + [schedule[-1]] * (count - len(schedule))


# Default profiles. Coder is exploratory + multi-sample; judges are near-greedy
# so their scores are reproducible and meaningfully comparable across candidates.
_DEFAULT_PROFILES: dict[CouncilRole, RoleSamplingProfile] = {
    CouncilRole.PLANNER: RoleSamplingProfile(temperature=0.4, top_p=0.95),
    CouncilRole.CODER: RoleSamplingProfile(
        temperature=0.3,
        top_p=0.95,
        n_samples=3,
        diverge_temperatures=(0.2, 0.5, 0.8),
    ),
    CouncilRole.REVIEWER: RoleSamplingProfile(temperature=0.1, top_p=0.9),
    CouncilRole.CHALLENGER: RoleSamplingProfile(temperature=0.2, top_p=0.9),
    CouncilRole.SYNTHESIZER: RoleSamplingProfile(temperature=0.3, top_p=0.95),
}

# Fallback for any role not explicitly registered (e.g. future roles).
_FALLBACK_PROFILE = RoleSamplingProfile(temperature=0.3)


def get_sampling_profile(role: CouncilRole) -> RoleSamplingProfile:
    """Return the sampling profile for *role* (never ``None``)."""
    return _DEFAULT_PROFILES.get(role, _FALLBACK_PROFILE)


def coder_sample_count(low_resource: bool, degraded_diversity: bool) -> int:
    """Resolve how many Coder samples to draw given runtime constraints.

    - ``low_resource``: collapse to a single sample (fast path / tight budget).
    - ``degraded_diversity``: all roles share one model, so lean *harder* on
      sampling to recover some diversity (bump the sample count).
    """
    base = get_sampling_profile(CouncilRole.CODER).n_samples
    if low_resource:
        return 1
    if degraded_diversity:
        return max(base, 3)
    return base
