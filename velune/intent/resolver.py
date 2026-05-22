"""Intent Hypothesis Resolver."""

from __future__ import annotations

import logging
from typing import List, Optional
from velune.intent.hypothesis import IntentHypothesis

logger = logging.getLogger("velune.intent.resolver")


class IntentResolver:
    """Arbitrates and resolves the active hypothesis, triggering user clarification loops when needed."""

    def __init__(self, confidence_threshold: float = 0.65) -> None:
        self.confidence_threshold = confidence_threshold

    def resolve(self, hypothesis: IntentHypothesis) -> bool:
        """
        Evaluate if the hypothesis meets the operational confidence threshold.
        Returns True if approved to execute autonomously, False if clarification is required.
        """
        if hypothesis.confidence >= self.confidence_threshold:
            logger.info("Hypothesis approved for execution (confidence %.2f >= %.2f)", hypothesis.confidence, self.confidence_threshold)
            return True
            
        logger.warning("Hypothesis confidence too low (%.2f < %.2f). User clarification is recommended.", hypothesis.confidence, self.confidence_threshold)
        return False

    def request_clarification_prompt(self, hypothesis: IntentHypothesis) -> str:
        """
        Generate a structured clarification request string to show to the user.
        """
        steps_str = "\n".join([f"  - {step}" for step in hypothesis.action_plan])
        return (
            f"I reconstructed your goal as: '{hypothesis.goal_description}'\n"
            f"Operational Category: {hypothesis.primary_category.capitalize()}\n"
            f"Target Files: {', '.join(hypothesis.target_files) if hypothesis.target_files else 'None'}\n\n"
            f"Proposed Action Plan:\n{steps_str}\n\n"
            f"Is this correct? (Yes/No/Provide more details)"
        )
class ActiveIntentTracker:
    """Tracks active intent changes and state completions during execution cycles."""

    def __init__(self, hypothesis: IntentHypothesis) -> None:
        self.hypothesis = hypothesis
        self.completed_steps: List[str] = []
        self.current_step_index = 0

    def get_current_step(self) -> Optional[str]:
        """Fetch the active step description from the action plan."""
        plan = self.hypothesis.action_plan
        if 0 <= self.current_step_index < len(plan):
            return plan[self.current_step_index]
        return None

    def mark_step_completed(self) -> None:
        """Advance the execution step pointer."""
        current = self.get_current_step()
        if current:
            self.completed_steps.append(current)
            self.current_step_index += 1
            logger.info("Completed intent step: %s", current)

    def is_fully_completed(self) -> bool:
        """Verify if all planned steps in the active intent have run."""
        return len(self.completed_steps) >= len(self.hypothesis.action_plan)
