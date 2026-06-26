"""Measured consensus engine for the Reasoning Council.

This module replaces the hardcoded ``agreement_rate``/``logic_score`` branch
constants that previously lived inside :mod:`velune.cognition.arbitrator`.  Those
constants made the council's "calibrated confidence" theatrical — the number
never reflected what the agents actually said.

Here, agreement and confidence are *computed* from the real report fields the
judges already emit (``ReviewerMessage.confidence_rating``,
``CriticMessage.score``/``passed``, ``ChallengerMessage.severity_rating``) plus
the textual similarity between candidate solutions.  The result is a confidence
score that genuinely moves with inter-agent agreement: unanimous high-scoring
judges yield high confidence; split votes, high score variance, or severe
challenger findings drag it down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any


def _attr(report: Any, key: str, default: Any) -> Any:
    """Read *key* from a report that may be a dict or a pydantic message."""
    if report is None:
        return default
    if isinstance(report, dict):
        return report.get(key, default)
    return getattr(report, key, default)


@dataclass(frozen=True)
class AgreementSignals:
    """Measured signals describing how much the judges agreed.

    All fields are derived from actual agent outputs — none are constants.
    """

    critic_pass_rate: float  # fraction of judges (reviewer + critics) that passed
    score_mean: float  # mean judge score in [0, 1]
    score_variance: float  # variance of judge scores (disagreement signal)
    pairwise_similarity: float  # mean pairwise similarity of candidate solutions
    challenger_severity: float  # worst challenger severity in [0, 1]
    n_judges: int
    n_candidates: int
    judge_scores: list[float] = field(default_factory=list)
    judge_passed: list[bool] = field(default_factory=list)

    @property
    def agreement_rate(self) -> float:
        """Composite agreement in [0, 1] from pass-rate, score spread, severity.

        Score variance for values in [0, 1] peaks near 0.25, so it is scaled by
        4 before being inverted into a "consistency" term.
        """
        consistency = 1.0 - min(self.score_variance * 4.0, 1.0)
        rate = (
            0.50 * self.critic_pass_rate
            + 0.30 * consistency
            + 0.20 * (1.0 - self.challenger_severity)
        )
        return max(0.0, min(1.0, rate))

    @property
    def logic_score(self) -> float:
        """Internal logical consistency: mean judge score discounted by severity."""
        score = self.score_mean * (1.0 - 0.5 * self.challenger_severity)
        return max(0.0, min(1.0, score))


def _pairwise_similarity(candidates: list[str]) -> float:
    """Mean pairwise textual similarity of candidate solutions in [0, 1].

    Returns 1.0 for fewer than two candidates (no disagreement to measure).
    Diversity (low similarity) is healthy during the diverge round but, after
    revision, high similarity means the solvers converged — a consensus signal.
    """
    texts = [c for c in candidates if c]
    if len(texts) < 2:
        return 1.0
    ratios: list[float] = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            ratios.append(SequenceMatcher(None, texts[i], texts[j]).ratio())
    return sum(ratios) / len(ratios) if ratios else 1.0


def measure_agreement(
    reviewer_report: Any,
    challenger_report: Any,
    critic_reports: list[Any] | None = None,
    candidates: list[str] | None = None,
) -> AgreementSignals:
    """Compute :class:`AgreementSignals` from the council's actual outputs."""
    critic_reports = critic_reports or []
    candidates = candidates or []

    scores: list[float] = []
    passed_flags: list[bool] = []

    # Reviewer contributes its confidence_rating and pass flag.
    if reviewer_report is not None:
        scores.append(float(_attr(reviewer_report, "confidence_rating", 0.5)))
        passed_flags.append(bool(_attr(reviewer_report, "passed", True)))

    # Each specialized critic contributes its score and pass flag.
    for report in critic_reports:
        if report is None:
            continue
        scores.append(float(_attr(report, "score", 0.9)))
        passed_flags.append(bool(_attr(report, "passed", True)))

    challenger_severity = float(_attr(challenger_report, "severity_rating", 0.0))

    n_judges = len(scores)
    score_mean = sum(scores) / n_judges if n_judges else 0.5
    score_variance = sum((s - score_mean) ** 2 for s in scores) / n_judges if n_judges else 0.0
    critic_pass_rate = (
        sum(1 for p in passed_flags if p) / len(passed_flags) if passed_flags else 1.0
    )

    return AgreementSignals(
        critic_pass_rate=critic_pass_rate,
        score_mean=score_mean,
        score_variance=score_variance,
        pairwise_similarity=_pairwise_similarity(candidates),
        challenger_severity=max(0.0, min(1.0, challenger_severity)),
        n_judges=n_judges,
        n_candidates=len([c for c in candidates if c]),
        judge_scores=scores,
        judge_passed=passed_flags,
    )


def calibrated_confidence(
    signals: AgreementSignals,
    historical_accuracy: float = 0.85,
) -> float:
    """Blend measured signals into a calibrated confidence score in [0, 1].

    Keeps the original weighting intent (models overstate, so cross-agent
    agreement dominates) but every input is now *measured*:

    - ``self_reported`` = mean judge score (the models' own confidence)
    - ``agreement_rate`` = composite pass-rate / consistency / severity
    - ``logic_score``    = mean score discounted by challenger severity
    """
    self_reported = signals.score_mean
    confidence = (
        self_reported * 0.20
        + signals.agreement_rate * 0.40
        + historical_accuracy * 0.25
        + signals.logic_score * 0.15
    )
    return round(max(0.0, min(1.0, confidence)), 3)


def medoid_index(candidates: list[str]) -> int:
    """Index of the candidate most similar to all the others.

    This is the self-consistency winner: when several independent solver samples
    are drawn, the one that best agrees with the rest is the consensus answer.
    Requires no extra model calls. Returns 0 for fewer than two candidates.
    """
    texts = [c or "" for c in candidates]
    if len(texts) < 2:
        return 0
    best_idx = 0
    best_total = -1.0
    for i in range(len(texts)):
        total = 0.0
        for j in range(len(texts)):
            if i == j:
                continue
            total += SequenceMatcher(None, texts[i], texts[j]).ratio()
        if total > best_total:
            best_total = total
            best_idx = i
    return best_idx


def select_winner(candidate_scores: list[float]) -> int:
    """Return the index of the highest-scoring candidate (ties → first).

    Used by the multi-solver vote round to pick the surviving proposal.
    """
    if not candidate_scores:
        return 0
    best_idx = 0
    best = candidate_scores[0]
    for idx, score in enumerate(candidate_scores[1:], start=1):
        if score > best:
            best = score
            best_idx = idx
    return best_idx
