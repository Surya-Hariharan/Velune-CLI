"""Arbitration engine evaluating council outputs, contradictions, and calibrated confidence."""

from __future__ import annotations

from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger("velune.cognition.arbitrator")


class ArbitrationResult:
    """Result of council deliberation arbitration."""

    def __init__(
        self,
        requires_human_review: bool,
        winning_claims: List[str],
        overall_confidence: float,
        flags: List[str],
        synthesis_instructions: str,
    ) -> None:
        self.requires_human_review = requires_human_review
        self.winning_claims = winning_claims
        self.overall_confidence = overall_confidence
        self.flags = flags
        self.synthesis_instructions = synthesis_instructions

    def to_dict(self) -> Dict[str, Any]:
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
        """Calculate calibrated confidence score to minimize overstatement bias."""
        calibrated_confidence = (
            self_reported * 0.20 +   # models overstate — low weight
            agreement_rate * 0.40 +  # cross-agent agreement is strong signal
            self.historical_accuracy * 0.25 +  # historical calibration
            logic_score * 0.15       # logical consistency score
        )
        return round(calibrated_confidence, 3)

    def arbitrate(
        self,
        plan_steps: List[str],
        coder_proposal: str,
        reviewer_report: Dict[str, Any],
        challenger_report: Dict[str, Any],
    ) -> ArbitrationResult:
        """Arbitrate the deliberations of the planner, coder, reviewer, and challenger."""
        logger.info("Council Arbitrator analyzing agent deliberations...")
        
        winning_claims: List[str] = []
        flags: List[str] = []

        # 1. Claim Extraction and Synthesis Instructions
        passed = reviewer_report.get("passed", True)
        reviewer_confidence = reviewer_report.get("confidence_rating", 0.7)
        reviewer_issues = reviewer_report.get("critical_issues", [])
        
        challenger_issues = challenger_report.get("failure_vectors", [])
        challenger_severity = challenger_report.get("severity_rating", 0.0)

        # 2. Contradiction Detection
        # E.g. Coder proposes complete success, but Reviewer or Challenger finds high severity problems
        has_critical_failures = len(reviewer_issues) > 0 or (challenger_severity > 0.7 and len(challenger_issues) > 0)
        
        # Calculate agreement rate
        # If reviewer passed and challenger severity is low, agreement is high. Otherwise low.
        if passed and challenger_severity < 0.3:
            agreement_rate = 0.95
            logic_score = 0.9
            winning_claims.append("Coder solution is syntactically sound and logical.")
        elif not passed and challenger_severity > 0.6:
            agreement_rate = 0.9  # Reviewer and Challenger agree there are bugs
            logic_score = 0.4     # High issues means poor internal logical consistency
            flags.append("CRITICAL_BUGS_DETECTED")
            winning_claims.append("Coder proposal contains bugs highlighted in reviews.")
        else:
            agreement_rate = 0.5  # Contradiction: Coder passed, but Challenger/Reviewer raised concerns
            logic_score = 0.6
            flags.append("CONTRADICTION_DETECTED")
            winning_claims.append("Proposed edits are partially sound but require refactoring.")

        # Calculate calibrated confidence using formula
        self_reported = (reviewer_confidence + (1.0 - challenger_severity)) / 2.0
        overall_confidence = self.calculate_calibrated_confidence(
            self_reported=self_reported,
            agreement_rate=agreement_rate,
            logic_score=logic_score
        )

        # 3. Assess if we need to escalate to the user
        requires_human_review = has_critical_failures or overall_confidence < 0.55

        # Compile synthesis guidance
        synthesis_instructions = "Consolidate the code patch and resolve the following:\n"
        for issue in reviewer_issues:
            synthesis_instructions += f"- Fix Reviewer critical issue: {issue}\n"
        for vector in challenger_issues:
            synthesis_instructions += f"- Mitigate Challenger failure vector: {vector}\n"

        if not reviewer_issues and not challenger_issues:
            synthesis_instructions += "- The solution was fully approved. Prepare clean summary of accomplishments."

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
