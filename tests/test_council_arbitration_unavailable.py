"""Judges that did not deliver a verdict abstain: they are excluded from the
agreement maths, named in the arbitration result, and cap confidence."""

from __future__ import annotations

import pytest

from velune.cognition.arbitrator import (
    MAJORITY_UNAVAILABLE_CONFIDENCE_CAP,
    NO_REVIEW_CONFIDENCE_CAP,
    UNAVAILABLE_CONFIDENCE_CAP,
    CouncilArbitrator,
)
from velune.cognition.consensus import calibrated_confidence, is_usable, measure_agreement
from velune.cognition.council.messages import ChallengerMessage, CriticMessage, ReviewerMessage


def _ok_reviewer() -> ReviewerMessage:
    return ReviewerMessage(passed=True, confidence_rating=0.9)


def _ok_critic(score: float = 0.9) -> CriticMessage:
    return CriticMessage(passed=True, score=score)


def _ok_challenger() -> ChallengerMessage:
    return ChallengerMessage(severity_rating=0.1)


def _full_set(**overrides):
    reports = {
        "reviewer_report": _ok_reviewer(),
        "challenger_report": _ok_challenger(),
        "scalability_report": _ok_critic(),
        "security_report": _ok_critic(),
        "performance_report": _ok_critic(),
        "maintainability_report": _ok_critic(),
    }
    reports.update(overrides)
    return reports


def _arbitrate(**reports):
    return CouncilArbitrator().arbitrate(plan_steps=[], coder_proposal="code", **reports)


# ── measure_agreement ────────────────────────────────────────────────────────


def test_is_usable_handles_messages_dicts_and_none():
    assert is_usable(_ok_reviewer())
    assert not is_usable(ReviewerMessage.degraded("unavailable", "x"))
    assert is_usable({"passed": True})
    assert not is_usable({"status": "unparseable"})
    assert not is_usable(None)


def test_abstaining_judges_do_not_count_towards_pass_rate_or_judges():
    signals = measure_agreement(
        reviewer_report=_ok_reviewer(),
        challenger_report=ChallengerMessage.degraded("unavailable", "x"),
        critic_reports=[
            _ok_critic(0.9),
            CriticMessage.degraded("unavailable", "x"),
            CriticMessage(passed=False, score=0.3),
        ],
    )
    assert signals.n_judges == 3  # reviewer + the two usable critics
    assert signals.critic_pass_rate == pytest.approx(2 / 3)
    assert signals.challenger_severity == 0.0
    assert 0.0 not in signals.judge_scores  # the abstainer's 0.0 never leaks in


def test_all_judges_abstaining_leaves_no_signal():
    signals = measure_agreement(
        reviewer_report=ReviewerMessage.degraded("unavailable", "x"),
        challenger_report=None,
        critic_reports=[CriticMessage.degraded("unavailable", "x")],
    )
    assert signals.n_judges == 0


# ── arbitrate ────────────────────────────────────────────────────────────────


def test_healthy_arbitration_is_unchanged():
    result = _arbitrate(**_full_set())
    assert result.flags == []
    signals = measure_agreement(
        reviewer_report=_ok_reviewer(),
        challenger_report=_ok_challenger(),
        critic_reports=[_ok_critic()] * 4,
        candidates=["code"],
    )
    assert result.overall_confidence == calibrated_confidence(signals, 0.85)
    assert result.requires_human_review is False


def test_one_unavailable_critic_flags_and_caps_without_demanding_review():
    result = _arbitrate(**_full_set(security_report=CriticMessage.degraded("unavailable", "x")))
    assert result.flags == ["JUDGE_UNAVAILABLE:security"]
    assert result.overall_confidence <= UNAVAILABLE_CONFIDENCE_CAP
    assert result.requires_human_review is False
    assert "Available specialized critics approved" in " ".join(result.winning_claims)


def test_unavailable_reviewer_demands_human_review():
    result = _arbitrate(**_full_set(reviewer_report=ReviewerMessage.degraded("unavailable", "x")))
    assert "JUDGE_UNAVAILABLE:reviewer" in result.flags
    assert result.requires_human_review is True
    assert result.overall_confidence <= MAJORITY_UNAVAILABLE_CONFIDENCE_CAP


def test_half_the_judges_unavailable_demands_human_review():
    down = CriticMessage.degraded("unavailable", "x")
    result = _arbitrate(
        **_full_set(
            security_report=down,
            performance_report=down,
            maintainability_report=down,
        )
    )
    assert result.requires_human_review is True
    assert result.overall_confidence <= MAJORITY_UNAVAILABLE_CONFIDENCE_CAP
    assert "NO_REVIEW" not in result.flags


def test_every_judge_unavailable_is_no_review():
    result = _arbitrate(
        reviewer_report=ReviewerMessage.degraded("unavailable", "x"),
        challenger_report=ChallengerMessage.degraded("unavailable", "x"),
        scalability_report=CriticMessage.degraded("unavailable", "x"),
        security_report=CriticMessage.degraded("unavailable", "x"),
        performance_report=CriticMessage.degraded("unavailable", "x"),
        maintainability_report=CriticMessage.degraded("unavailable", "x"),
    )
    assert "NO_REVIEW" in result.flags
    assert result.requires_human_review is True
    assert result.overall_confidence <= NO_REVIEW_CONFIDENCE_CAP


def test_unavailable_seats_argument_names_re_review_failures():
    failing = CriticMessage(passed=False, issues=["injection"], score=0.2)
    result = _arbitrate(
        **_full_set(security_report=failing),
        unavailable_seats=["security(re-review)"],
    )
    assert "JUDGE_UNAVAILABLE:security(re-review)" in result.flags
    assert result.requires_human_review is True  # the objection is still outstanding
    assert result.overall_confidence <= UNAVAILABLE_CONFIDENCE_CAP


def test_abstaining_reports_never_produce_critic_issues_in_the_instructions():
    result = _arbitrate(
        **_full_set(
            challenger_report=ChallengerMessage.degraded("unparseable", "x"),
            security_report=CriticMessage.degraded("unavailable", "x"),
        )
    )
    assert "Challenger failure vector" not in result.synthesis_instructions
    assert "Critic issue" not in result.synthesis_instructions


def test_minimal_tier_without_reviewer_is_not_reported_as_an_unavailable_judge():
    result = _arbitrate(reviewer_report=None, challenger_report=None)
    assert not any(flag.startswith("JUDGE_UNAVAILABLE") for flag in result.flags)
    assert "NO_REVIEW" not in result.flags
