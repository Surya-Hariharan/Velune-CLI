"""Explicit, enforceable execution contracts for each council tier.

Before this module, tier composition was implied by scattered ``tier_level >= N``
comparisons inside ``CouncilOrchestrator._execute_tiered``. Those comparisons
disagreed with :class:`~velune.cognition.council.tiers.CouncilTier`'s own
docstrings, and nothing checked them against each other, so the documented
contract and the runtime behaviour drifted apart silently:

``CouncilTier.STANDARD`` documented itself as "Coder + Reviewer", but the
``>= 2`` / ``>= 3`` gates activated a Planner, three self-consistency Coder
samples, four critics, and a Synthesizer — ten provider calls for a tier
meant to sit two rungs below the full council. STANDARD had become FULL minus
one challenger call, collapsing a four-rung ladder into "cheap, cheap, expensive,
expensive".

The contracts below are the single source of truth. ``_execute_tiered`` reads
seat activation from them instead of re-deriving it from integer comparisons,
and :func:`verify_trace_against_contract` checks a finished run's provider-call
ledger against the declared bounds, so a future drift fails a test instead of
quietly costing tokens.

Composition rationale (derived from the existing design, not invented):

- **INSTANT** — one Coder pass. Read-only queries and explanations. Unchanged.
- **MINIMAL** — Planner + Coder, no judgment. Unchanged.
- **STANDARD** — the documented "Coder + Reviewer", plus the Planner that
  ``docs/03-cognitive-architecture.md`` already assigns to STANDARD, plus the
  Synthesizer needed to turn plan + code + review into an answer. Single Coder
  sample: multi-sample self-consistency is deliberation depth, which is what
  distinguishes FULL. The four critics are FULL's "all agents".
- **FULL** — every seat, three Coder samples, multi-turn debate. Unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from velune.cognition.council.tiers import CouncilTier

# Canonical seat names. These are *seats*, not
# :class:`~velune.models.specializations.CouncilRole` values: the security,
# performance, and maintainability critics all execute on the REVIEWER role
# descriptor, so role identity cannot distinguish them in a trace.
SEAT_PLANNER = "planner"
SEAT_CODER = "coder"
SEAT_REVIEWER = "reviewer"
SEAT_CHALLENGER = "challenger"
SEAT_SCALABILITY = "scalability_critic"
SEAT_SECURITY = "security_critic"
SEAT_PERFORMANCE = "performance_critic"
SEAT_MAINTAINABILITY = "maintainability_critic"
SEAT_SYNTHESIZER = "synthesizer"

ALL_CRITIC_SEATS = (
    SEAT_SCALABILITY,
    SEAT_SECURITY,
    SEAT_PERFORMANCE,
    SEAT_MAINTAINABILITY,
)


@dataclass(frozen=True)
class TierContract:
    """The execution contract a tier promises to honour.

    Attributes:
        tier: The tier this contract governs.
        required_seats: Seats that must execute. A run missing one is a
            contract violation, not a degraded result.
        optional_seats: Seats that execute only when their activation
            condition holds (currently: debate re-entry).
        coder_samples: Independent Coder candidates drawn in the diverge
            round. ``1`` disables self-consistency for this tier.
        max_debate_turns: Upper bound on revision rounds. ``0`` forbids
            re-execution of any seat outside retry/fallback.
        escalation: Human-readable condition under which this tier hands off
            to a deeper one. ``""`` means the tier never escalates.
        retry_owner: Which layer owns retrying a failed call for this tier.
            Always ``"provider"`` — see ``velune/providers/retrying.py`` and
            the retry-ownership note in ``CouncilOrchestrator``.
        token_budget: Soft ceiling on total tokens across the run, used for
            budgeting and reporting rather than hard truncation.
    """

    tier: CouncilTier
    required_seats: tuple[str, ...]
    optional_seats: tuple[str, ...] = ()
    coder_samples: int = 1
    max_debate_turns: int = 0
    escalation: str = ""
    retry_owner: str = "provider"
    token_budget: int = 8_000

    @property
    def uses_planner(self) -> bool:
        return SEAT_PLANNER in self.required_seats

    @property
    def uses_reviewer(self) -> bool:
        return SEAT_REVIEWER in self.required_seats

    @property
    def uses_synthesizer(self) -> bool:
        return SEAT_SYNTHESIZER in self.required_seats

    @property
    def critic_seats(self) -> tuple[str, ...]:
        return tuple(s for s in self.required_seats if s in ALL_CRITIC_SEATS)

    def activates(self, seat: str) -> bool:
        """Return True if *seat* may execute under this contract."""
        return seat in self.required_seats or seat in self.optional_seats

    @property
    def min_provider_calls(self) -> int:
        """Fewest provider calls a clean run can make (no debate, no retries)."""
        return len(self.required_seats) - 1 + self.coder_samples

    @property
    def max_provider_calls(self) -> int:
        """Most provider calls a clean run can make, debate included.

        Excludes provider-owned retries and fallbacks, which are failure
        handling rather than deliberation and are bounded separately by
        ``RetryingProvider``.
        """
        # Each debate turn re-runs the Coder plus any judging seat that
        # objected — bounded above by every judging seat re-running.
        judges = len([s for s in self.required_seats if s not in (SEAT_PLANNER, SEAT_CODER)])
        return self.min_provider_calls + self.max_debate_turns * (1 + judges)

    def summary(self) -> str:
        return (
            f"{self.tier.value}: seats={'+'.join(self.required_seats)} "
            f"coder_samples={self.coder_samples} max_debate_turns={self.max_debate_turns} "
            f"provider_calls={self.min_provider_calls}..{self.max_provider_calls}"
        )


TIER_CONTRACTS: dict[CouncilTier, TierContract] = {
    CouncilTier.INSTANT: TierContract(
        tier=CouncilTier.INSTANT,
        required_seats=(SEAT_CODER,),
        coder_samples=1,
        max_debate_turns=0,
        escalation="",
        token_budget=4_000,
    ),
    CouncilTier.MINIMAL: TierContract(
        tier=CouncilTier.MINIMAL,
        required_seats=(SEAT_PLANNER, SEAT_CODER),
        coder_samples=1,
        max_debate_turns=0,
        escalation="",
        token_budget=8_000,
    ),
    CouncilTier.STANDARD: TierContract(
        tier=CouncilTier.STANDARD,
        required_seats=(SEAT_PLANNER, SEAT_CODER, SEAT_REVIEWER, SEAT_SYNTHESIZER),
        coder_samples=1,
        max_debate_turns=1,
        escalation="reviewer raises critical issues that one revision turn does not clear",
        token_budget=24_000,
    ),
    CouncilTier.FULL: TierContract(
        tier=CouncilTier.FULL,
        required_seats=(
            SEAT_PLANNER,
            SEAT_CODER,
            SEAT_REVIEWER,
            SEAT_CHALLENGER,
            SEAT_SCALABILITY,
            SEAT_SECURITY,
            SEAT_PERFORMANCE,
            SEAT_MAINTAINABILITY,
            SEAT_SYNTHESIZER,
        ),
        coder_samples=3,
        max_debate_turns=3,
        escalation="",
        token_budget=120_000,
    ),
}


def contract_for(tier: CouncilTier) -> TierContract:
    """Return the execution contract governing *tier*."""
    return TIER_CONTRACTS[tier]


@dataclass
class ContractViolation:
    """A specific way a finished run departed from its tier contract."""

    kind: str
    detail: str


@dataclass
class ContractVerdict:
    """Result of checking a completed run against its contract."""

    tier: CouncilTier
    total_calls: int
    violations: list[ContractViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def render(self) -> str:
        if self.ok:
            return f"{self.tier.value}: contract honoured ({self.total_calls} provider calls)"
        lines = [f"{self.tier.value}: {len(self.violations)} contract violation(s)"]
        lines.extend(f"  - [{v.kind}] {v.detail}" for v in self.violations)
        return "\n".join(lines)


def verify_trace_against_contract(trace, tier: CouncilTier) -> ContractVerdict:
    """Check a :class:`~velune.cognition.execution_trace.RequestTrace` against *tier*'s contract.

    Retries and fallbacks are excluded from the deliberation count: they are
    provider-owned failure handling, bounded by ``RetryingProvider``, not
    council deliberation. Counting them here would make a flaky network look
    like a contract breach.
    """
    from velune.cognition.execution_trace import CallReason

    contract = contract_for(tier)
    verdict = ContractVerdict(tier=tier, total_calls=len(trace.calls))

    deliberations = [
        c for c in trace.calls if c.reason not in (CallReason.RETRY, CallReason.FALLBACK)
    ]

    seats_seen = {c.seat for c in deliberations}
    for seat in contract.required_seats:
        if seat not in seats_seen:
            verdict.violations.append(
                ContractViolation("missing_required_seat", f"{seat} never executed")
            )
    for seat in sorted(seats_seen):
        if not contract.activates(seat):
            verdict.violations.append(
                ContractViolation("unauthorized_seat", f"{seat} is not part of this tier")
            )

    if len(deliberations) > contract.max_provider_calls:
        verdict.violations.append(
            ContractViolation(
                "call_budget_exceeded",
                f"{len(deliberations)} deliberations > contract maximum "
                f"{contract.max_provider_calls}",
            )
        )

    for call in trace.unexplained_calls():
        verdict.violations.append(
            ContractViolation(
                "unexplained_repeat",
                f"{call.call_id}: {call.seat} executed again with reason=primary",
            )
        )

    coder_primary = [
        c for c in deliberations if c.seat == SEAT_CODER and c.reason is not CallReason.REVISION
    ]
    if len(coder_primary) > contract.coder_samples:
        verdict.violations.append(
            ContractViolation(
                "sample_budget_exceeded",
                f"{len(coder_primary)} coder samples > contract {contract.coder_samples}",
            )
        )

    return verdict
