"""Arbitration engine evaluating council outputs, contradictions, and calibrated confidence."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("velune.cognition.arbitrator")


class ArbitrationResult:
    """Result of council deliberation arbitration."""

    def __init__(
        self,
        requires_human_review: bool,
        winning_claims: list[str],
        overall_confidence: float,
        flags: list[str],
        synthesis_instructions: str,
    ) -> None:
        self.requires_human_review = requires_human_review
        self.winning_claims = winning_claims
        self.overall_confidence = overall_confidence
        self.flags = flags
        self.synthesis_instructions = synthesis_instructions

    def to_dict(self) -> dict[str, Any]:
        return {
            "requires_human_review": self.requires_human_review,
            "winning_claims": self.winning_claims,
            "overall_confidence": self.overall_confidence,
            "flags": self.flags,
            "synthesis_instructions": self.synthesis_instructions,
        }


class CouncilArbitrator:
    """Calibrates confidence scores and resolves logical contradictions between agent outputs."""

    def __init__(self, historical_accuracy: float = 0.85) -> None:
        self.historical_accuracy = historical_accuracy

    def calculate_calibrated_confidence(
        self,
        self_reported: float,
        agreement_rate: float,
        logic_score: float,
    ) -> float:
        """Calculate calibrated confidence score to minimize overstatement bias.

        Retained for backward compatibility; the live path now delegates to
        :func:`velune.cognition.consensus.calibrated_confidence` with *measured*
        signals rather than the hardcoded inputs this method used to receive.
        """
        calibrated_confidence = (
            self_reported * 0.20  # models overstate — low weight
            + agreement_rate * 0.40  # cross-agent agreement is strong signal
            + self.historical_accuracy * 0.25  # historical calibration
            + logic_score * 0.15  # logical consistency score
        )
        return round(calibrated_confidence, 3)

    def arbitrate(
        self,
        plan_steps: list[str],
        coder_proposal: str,
        reviewer_report: Any,
        challenger_report: Any,
        scalability_report: Any | None = None,
        security_report: Any | None = None,
        performance_report: Any | None = None,
        maintainability_report: Any | None = None,
        critic_weights: dict[str, float] | None = None,
        shi: float | None = None,
        candidates: list[str] | None = None,
    ) -> ArbitrationResult:
        """Arbitrate the deliberations of the planner, coder, reviewer, challenger, and specialized critics."""
        logger.info("Council Arbitrator analyzing agent deliberations...")

        winning_claims: list[str] = []
        flags: list[str] = []

        # 1. Claim Extraction and Synthesis Instructions
        if isinstance(reviewer_report, dict):
            passed = reviewer_report.get("passed", True)
            reviewer_issues = reviewer_report.get("critical_issues", [])
        elif reviewer_report is not None:
            passed = reviewer_report.passed
            reviewer_issues = reviewer_report.critical_issues
        else:
            passed = True
            reviewer_issues = ["Reviewer unavailable"]

        if isinstance(challenger_report, dict):
            challenger_issues = challenger_report.get("failure_vectors", [])
            challenger_severity = challenger_report.get("severity_rating", 0.0)
        elif challenger_report is not None:
            challenger_issues = challenger_report.failure_vectors
            challenger_severity = challenger_report.severity_rating
        else:
            challenger_issues = []
            challenger_severity = 0.0

        # 2. Extract specialized critic properties if they are provided
        critic_reports = []
        if scalability_report:
            critic_reports.append(("Scalability", scalability_report))
        if security_report:
            critic_reports.append(("Security", security_report))
        if performance_report:
            critic_reports.append(("Performance", performance_report))
        if maintainability_report:
            critic_reports.append(("Maintainability", maintainability_report))

        # Check if specialized critics failed
        failed_critics = []
        critic_issues = []
        for name, report in critic_reports:
            if isinstance(report, dict):
                is_passed = report.get("passed", True)
                issues = report.get("issues", [])
            else:
                is_passed = report.passed
                issues = report.issues

            if not is_passed:
                failed_critics.append(name)
            for issue in issues:
                critic_issues.append(f"{name} Critic: {issue}")

        # 3. Contradiction Detection
        # E.g. Coder proposes complete success, but Reviewer, Challenger, or Critics find problems
        has_critical_failures = (
            len(reviewer_issues) > 0
            or (challenger_severity > 0.7 and len(challenger_issues) > 0)
            or len(failed_critics) > 0
        )

        # Measure agreement and confidence from the agents' ACTUAL outputs
        # (no hardcoded agreement/logic constants). See velune.cognition.consensus.
        from velune.cognition.consensus import calibrated_confidence, measure_agreement

        all_passed = passed and (len(failed_critics) == 0)

        signals = measure_agreement(
            reviewer_report=reviewer_report,
            challenger_report=challenger_report,
            critic_reports=[report for _, report in critic_reports],
            candidates=candidates or [coder_proposal],
        )
        overall_confidence = calibrated_confidence(signals, self.historical_accuracy)

        # The if/elif/else now drives only the qualitative narrative (winning
        # claims + flags), not the numeric confidence.
        if all_passed and challenger_severity < 0.3:
            winning_claims.append("Coder solution is syntactically sound and logical.")
            if critic_reports:
                winning_claims.append(
                    "All specialized critics approved the proposed implementation."
                )
        elif not all_passed and challenger_severity > 0.6:
            flags.append("CRITICAL_BUGS_DETECTED")
            winning_claims.append("Coder proposal contains bugs and architectural violations.")
        else:
            flags.append("CONTRADICTION_DETECTED")
            winning_claims.append("Proposed edits are partially sound but require refactoring.")

        if critic_weights:
            logger.debug("Critic weights supplied (informational): %s", critic_weights)

        logger.info(
            "Measured agreement: pass_rate=%.2f score_mean=%.2f variance=%.3f "
            "agreement=%.2f logic=%.2f over %d judges",
            signals.critic_pass_rate,
            signals.score_mean,
            signals.score_variance,
            signals.agreement_rate,
            signals.logic_score,
            signals.n_judges,
        )

        # Calculate variable passing thresholds scaled by subsystem risk (SHI)
        confidence_threshold = 0.55
        if shi is not None:
            if shi < 0.6:
                confidence_threshold = 0.75
            elif shi < 0.8:
                confidence_threshold = 0.65

        # 4. Assess if we need to escalate to the user
        requires_human_review = has_critical_failures or overall_confidence < confidence_threshold

        # Compile synthesis guidance
        synthesis_instructions = "Consolidate the code patch and resolve the following:\n"
        for issue in reviewer_issues:
            synthesis_instructions += f"- Fix Reviewer critical issue: {issue}\n"
        for vector in challenger_issues:
            synthesis_instructions += f"- Mitigate Challenger failure vector: {vector}\n"
        for issue in critic_issues:
            synthesis_instructions += f"- Resolve Critic issue: {issue}\n"

        if not reviewer_issues and not challenger_issues and not critic_issues:
            synthesis_instructions += (
                "- The solution was fully approved. Prepare clean summary of accomplishments."
            )

        logger.info(
            "Arbitration completed. Confidence: %.2f. Requires human review: %s",
            overall_confidence,
            requires_human_review,
        )

        return ArbitrationResult(
            requires_human_review=requires_human_review,
            winning_claims=winning_claims,
            overall_confidence=overall_confidence,
            flags=flags,
            synthesis_instructions=synthesis_instructions,
        )
