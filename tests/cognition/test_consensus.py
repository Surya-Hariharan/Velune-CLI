"""Tests for the measured consensus engine (velune.cognition.consensus).

These lock in the property that confidence is *measured* — it moves with real
inter-agent agreement rather than the hardcoded constants the arbitrator used to
emit.
"""

from __future__ import annotations

from velune.cognition.arbitrator import CouncilArbitrator
from velune.cognition.consensus import (
    calibrated_confidence,
    measure_agreement,
    select_winner,
)
from velune.cognition.council.messages import (
    ChallengerMessage,
    CriticMessage,
    ReviewerMessage,
)


def _unanimous_pass() -> tuple[ReviewerMessage, ChallengerMessage, list[CriticMessage]]:
    reviewer = ReviewerMessage(passed=True, confidence_rating=0.95, critical_issues=[])
    challenger = ChallengerMessage(severity_rating=0.05)
    critics = [
        CriticMessage(passed=True, score=0.95),
        CriticMessage(passed=True, score=0.92),
        CriticMessage(passed=True, score=0.9),
    ]
    return reviewer, challenger, critics


def _split_contradiction() -> tuple[ReviewerMessage, ChallengerMessage, list[CriticMessage]]:
    reviewer = ReviewerMessage(passed=False, confidence_rating=0.4, critical_issues=["bug"])
    challenger = ChallengerMessage(severity_rating=0.85, failure_vectors=["race"])
    critics = [
        CriticMessage(passed=False, score=0.2, issues=["O(n^2)"]),
        CriticMessage(passed=True, score=0.85),
        CriticMessage(passed=False, score=0.3, issues=["injection"]),
    ]
    return reviewer, challenger, critics


def test_unanimous_agreement_yields_high_confidence() -> None:
    reviewer, challenger, critics = _unanimous_pass()
    signals = measure_agreement(reviewer, challenger, critics, candidates=["sol"])
    assert signals.critic_pass_rate == 1.0
    assert signals.agreement_rate > 0.85
    conf = calibrated_confidence(signals)
    assert conf > 0.8


def test_contradiction_yields_low_confidence() -> None:
    reviewer, challenger, critics = _split_contradiction()
    signals = measure_agreement(reviewer, challenger, critics, candidates=["sol"])
    assert signals.critic_pass_rate < 0.5  # reviewer + 2 critics failed
    assert signals.challenger_severity > 0.8
    conf = calibrated_confidence(signals)
    assert conf < 0.7


def test_confidence_is_monotonic_in_agreement() -> None:
    high = calibrated_confidence(measure_agreement(*_unanimous_pass(), candidates=["x"]))
    low = calibrated_confidence(measure_agreement(*_split_contradiction(), candidates=["x"]))
    assert high > low


def test_confidence_not_a_hardcoded_constant() -> None:
    """Two different agreement states must produce two different numbers."""
    a = calibrated_confidence(measure_agreement(*_unanimous_pass(), candidates=["x"]))
    b = calibrated_confidence(measure_agreement(*_split_contradiction(), candidates=["x"]))
    assert a != b
    # And neither equals the old fabricated 0.95/0.5 branch constants by accident.
    assert abs(a - 0.95) > 1e-9 or abs(b - 0.5) > 1e-9


def test_select_winner_picks_highest_score() -> None:
    assert select_winner([0.3, 0.9, 0.5]) == 1
    assert select_winner([0.8, 0.8, 0.4]) == 0  # tie -> first
    assert select_winner([]) == 0


def test_arbitrator_delegates_to_measured_confidence() -> None:
    """End-to-end: arbitrate() must produce different confidence for pass vs fail."""
    arb = CouncilArbitrator()
    rev_ok, ch_ok, critics_ok = _unanimous_pass()
    rev_bad, ch_bad, critics_bad = _split_contradiction()

    good = arb.arbitrate(
        plan_steps=[],
        coder_proposal="def f(): return 1",
        reviewer_report=rev_ok,
        challenger_report=ch_ok,
        scalability_report=critics_ok[0],
        security_report=critics_ok[1],
        performance_report=critics_ok[2],
    )
    bad = arb.arbitrate(
        plan_steps=[],
        coder_proposal="def f(): return 1",
        reviewer_report=rev_bad,
        challenger_report=ch_bad,
        scalability_report=critics_bad[0],
        security_report=critics_bad[1],
        performance_report=critics_bad[2],
    )
    assert good.overall_confidence > bad.overall_confidence
    assert bad.requires_human_review is True
