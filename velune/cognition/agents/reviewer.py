"""Production ReviewerAgent with decision logic and review cycle enforcement."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import ReviewerMessage
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider
from velune.cognition.state import ReviewDecision

if TYPE_CHECKING:
    from velune.cognition.state import CouncilState

logger = logging.getLogger("velune.cognition.agents.reviewer")

REVIEWER_SYSTEM_PROMPT = """You are the Senior Code Reviewer for the Velune Reasoning Council.
Your role is to perform quality, safety, and regression audits on proposed code changes.

Analyze the implementation for:
- Logical flaws or edge-case regressions.
- Syntax errors or type mismatches.
- Security vulnerabilities (command injection, directory traversal, etc).
- Performance bottlenecks or redundant operations.
- Alignment with the original task and plan.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "passed": true/false,
  "critical_issues": ["Issue 1", "Issue 2"],
  "suggestions": ["Suggestion 1"],
  "confidence_rating": 0.85
}
"""


class ReviewerAgent(BaseCouncilAgent):
    """Production Reviewer Agent with review decision logic."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.REVIEWER,
            model=model,
            provider=provider,
            system_prompt=REVIEWER_SYSTEM_PROMPT,
        )

    async def review_proposal(
        self,
        task: str,
        proposal: str,
        context: str,
        state: CouncilState,
    ) -> tuple[ReviewDecision, str]:
        """Review code proposal and emit decision with optional refinement notes.

        Args:
            task: Original task description
            proposal: Proposed code from Coder
            context: Repository context
            state: CouncilState to write decision into

        Returns:
            (ReviewDecision, notes_for_coder)
            - If APPROVE: notes may be empty
            - If REJECT: notes describe blocking issues
            - If REVISE: notes guide refinement for next coder cycle

        Raises:
            TimeoutError: If reviewer_timeout_seconds exceeded
            ValueError: If budget exhausted before execution
        """
        if state.is_budget_exhausted():
            raise ValueError(f"Wall-clock budget exhausted before Reviewer could run")

        remaining = state.remaining_budget_seconds()
        timeout = min(state.budget.reviewer_timeout_seconds, int(remaining))

        logger.info(
            "Reviewer starting proposal audit (timeout: %ds, wall budget: %.1fs remaining)",
            timeout,
            remaining,
        )

        try:
            import asyncio

            user_messages = [
                {
                    "role": "user",
                    "content": (
                        f"TASK: {task}\n\n"
                        f"PROPOSED IMPLEMENTATION:\n{proposal}\n\n"
                        f"CONTEXT:\n{context}"
                    ),
                }
            ]

            # Call reviewer with timeout
            result = await asyncio.wait_for(
                self.typed_deliberate(user_messages, ReviewerMessage, temperature=0.1),
                timeout=timeout,
            )

            if result.parse_error:
                logger.warning("Reviewer parse error: %s", result.parse_error)
                # Conservative: treat parse errors as rejections
                decision = ReviewDecision.REJECT
                notes = f"Review execution error: {result.parse_error}. Please resubmit."
            else:
                decision, notes = self._make_decision(result, state)

            state.set_reviewer_output(decision, notes)
            logger.info(
                "Reviewer completed: decision=%s, cycle=%d/%d",
                decision.value,
                state.review_cycle_count,
                state.budget.max_review_cycles,
            )

            return decision, notes

        except asyncio.TimeoutError:
            logger.error("Reviewer timed out after %ds", timeout)
            raise

    def _make_decision(
        self, review: ReviewerMessage, state: CouncilState
    ) -> tuple[ReviewDecision, str]:
        """Convert reviewer message into structured decision + notes.

        Logic:
        - If passed with high confidence: APPROVE
        - If critical issues + cycles remaining: REVISE (with notes)
        - If critical issues + cycles exhausted: REJECT (with notes)
        - If passed but low confidence: REVISE if cycles remain, else APPROVE
        """
        notes = ""

        if review.critical_issues:
            notes = "Critical issues found:\n"
            for issue in review.critical_issues:
                notes += f"- {issue}\n"

            if review.suggestions:
                notes += "\nRefinement guidance:\n"
                for sugg in review.suggestions:
                    notes += f"- {sugg}\n"

        if review.passed and review.confidence_rating >= 0.8:
            # High confidence approval
            logger.info("Reviewer: APPROVE (confidence=%.2f)", review.confidence_rating)
            return ReviewDecision.APPROVE, notes

        if review.critical_issues:
            if state.review_cycle_count < state.budget.max_review_cycles:
                # Cycles remaining: request refinement
                logger.info(
                    "Reviewer: REVISE (cycles=%d/%d)",
                    state.review_cycle_count,
                    state.budget.max_review_cycles,
                )
                return ReviewDecision.REVISE, notes

            else:
                # Cycles exhausted: reject with warning
                logger.warning(
                    "Reviewer: REJECT (max cycles exhausted, %d/%d)",
                    state.review_cycle_count,
                    state.budget.max_review_cycles,
                )
                return ReviewDecision.REJECT, notes

        if not review.passed:
            if state.review_cycle_count < state.budget.max_review_cycles:
                logger.info("Reviewer: REVISE (failed but cycles available)")
                return ReviewDecision.REVISE, notes
            else:
                logger.warning("Reviewer: REJECT (failed, no cycles remaining)")
                return ReviewDecision.REJECT, notes

        if (
            review.confidence_rating < 0.8
            and state.review_cycle_count < state.budget.max_review_cycles
        ):
            logger.info("Reviewer: REVISE (low confidence=%.2f)", review.confidence_rating)
            return ReviewDecision.REVISE, notes

        # Default: approve
        logger.info("Reviewer: APPROVE (default, confidence=%.2f)", review.confidence_rating)
        return ReviewDecision.APPROVE, notes
