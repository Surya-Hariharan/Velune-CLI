from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.cognition.council.messages import ChallengerMessage
    from velune.cognition.state import ReviewDecision


@dataclass
class ScoredProposal:
    """A debate-ranked proposal with score and audit trail."""

    content: str
    score: float
    rank: int = 0
    objections: list[str] = field(default_factory=list)
    audit_reports: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DebateConfig:
    """Controls Council debate dynamics."""

    max_turns: int = 3
    min_turns: int = 1
    convergence_threshold: float = 0.0  # Stop if 0 objections remain
    severity_turn_boost: float = 0.8  # If avg severity > this, add extra turn
    critical_issue_hard_stop: bool = True  # Stop if reviewer flags critical security issue


def calculate_max_debate_turns(
    initial_objections: list[str],
    critic_reports: dict,
    task_complexity: str,  # "simple" | "structural"
    base_max: int = 3,
) -> int:
    """
    Dynamically calculate max debate turns based on:
    - Number and severity of objections
    - Task structural complexity
    - Specific critic failure patterns
    """
    if not initial_objections:
        return 0  # No debate needed

    turns = base_max

    # Security objections always get extra turn
    security_failed = not critic_reports.get("security", {}).get("passed", True)
    if security_failed:
        turns = max(turns, 4)

    # High challenger severity adds turn
    challenger_severity = critic_reports.get("challenger", {}).get("severity_rating", 0.0)
    if challenger_severity > 0.8:
        turns += 1

    # Simple tasks cap lower
    if task_complexity == "simple":
        turns = min(turns, 2)

    return min(turns, 5)  # Hard cap at 5


class DebateSession:
    """Score and rank council proposals using challenger and reviewer findings.

    Does not make LLM calls — pure scoring pass executed after all agents have
    deliberated.  Results feed directly into the Synthesizer.
    """

    def __init__(self, config: DebateConfig) -> None:
        self.config = config

    def run(
        self,
        proposals: list[str],
        challenger_reports: list[ChallengerMessage],
        reviewer_decision: ReviewDecision,
        reviewer_notes: str = "",
        task_complexity: str = "structural",
    ) -> list[ScoredProposal]:
        """Score *proposals* and return them ranked by score descending.

        Args:
            proposals: Proposal text strings (one per candidate).
            challenger_reports: One ChallengerMessage per proposal.
            reviewer_decision: Final ReviewDecision from the reviewer agent.
            reviewer_notes: Reviewer notes (appended to audit trail).
            task_complexity: ``"simple"`` or ``"structural"`` — affects turn cap.

        Returns:
            List of ScoredProposal sorted by score descending (rank 0 = best).
        """
        from velune.cognition.state import ReviewDecision

        scored: list[ScoredProposal] = []

        for idx, (proposal, challenge) in enumerate(
            zip(proposals, challenger_reports, strict=False)
        ):
            objections: list[str] = list(challenge.failure_vectors) + list(
                challenge.assumptions_challenged
            )
            severity: float = challenge.severity_rating

            critic_reports: dict[str, Any] = {
                "challenger": {"severity_rating": severity},
                "security": {"passed": reviewer_decision != ReviewDecision.REJECT},
            }
            debate_turns = calculate_max_debate_turns(objections, critic_reports, task_complexity)

            # Score: start full, penalise for severity and objection count
            score = 1.0
            score -= severity * 0.4
            score -= min(len(objections) * 0.05, 0.3)
            if reviewer_decision == ReviewDecision.REJECT:
                score *= 0.3

            audit_reports: list[dict[str, Any]] = [
                {
                    "type": "challenger",
                    "severity_rating": severity,
                    "assumptions_challenged": list(challenge.assumptions_challenged),
                    "failure_vectors": list(challenge.failure_vectors),
                    "debate_turns": debate_turns,
                },
                {
                    "type": "reviewer",
                    "decision": reviewer_decision.value,
                    "notes": reviewer_notes,
                },
            ]

            scored.append(
                ScoredProposal(
                    content=proposal,
                    score=max(0.0, min(1.0, score)),
                    rank=idx,
                    objections=objections,
                    audit_reports=audit_reports,
                )
            )

        scored.sort(key=lambda p: p.score, reverse=True)
        for rank_idx, sp in enumerate(scored):
            sp.rank = rank_idx

        return scored
