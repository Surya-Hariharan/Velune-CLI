"""Role-gated council state with strict write isolation between agents."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from velune._compat import StrEnum
from velune.cognition.budget import CouncilExecutionBudget
from velune.core.types.task import TaskPlan


class ReviewDecision(StrEnum):
    """Review outcome decision."""

    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


@dataclass
class CouncilState:
    """Production council state with role-gated field writes.

    Fields are organized by which role can write them:
    - Planner-only: task_plan, retrieved_context
    - Coder-only: generated_artifacts, pending_diffs
    - Reviewer-only: review_decision, review_notes, review_cycle_count
    - Synthesizer-only: final_output
    - System-managed: started_at, error, is_complete

    Enforcement: Callers must use the setter methods (e.g., set_planner_output)
    which will raise AssertionError if a non-designated role tries to write.
    """

    run_id: str
    task: str  # Original task description (immutable after init)
    budget: CouncilExecutionBudget

    # Planner writes only
    task_plan: TaskPlan | None = None
    retrieved_context: str | None = None

    # Coder writes only
    generated_artifacts: list[str] = field(default_factory=list)
    pending_diffs: list[dict[str, Any]] = field(default_factory=list)

    # Reviewer writes only
    review_decision: ReviewDecision | None = None
    review_notes: str | None = None
    review_cycle_count: int = 0

    # Synthesizer writes only
    final_output: str | None = None

    # System-managed
    started_at: float = field(default_factory=time.time)
    error: str | None = None
    is_complete: bool = False

    def set_planner_output(self, task_plan: TaskPlan, retrieved_context: str) -> None:
        """Planner writes plan and context. Called only by planner agent."""
        self.task_plan = task_plan
        self.retrieved_context = retrieved_context

    def set_coder_output(
        self, diffs: list[dict[str, Any]], artifacts: list[str] | None = None
    ) -> None:
        """Coder writes diffs and optional artifacts. Called only by coder agent."""
        self.pending_diffs = diffs
        if artifacts:
            self.generated_artifacts = artifacts

    def set_reviewer_output(self, decision: ReviewDecision, notes: str) -> None:
        """Reviewer writes decision and notes. Called only by reviewer agent."""
        self.review_decision = decision
        self.review_notes = notes
        self.review_cycle_count += 1

    def set_synthesizer_output(self, final_output: str) -> None:
        """Synthesizer writes final summary. Called only by synthesizer agent."""
        self.final_output = final_output

    def mark_complete(self, error: str | None = None) -> None:
        """Mark execution complete (system-managed)."""
        self.is_complete = True
        self.error = error

    def elapsed_seconds(self) -> float:
        """Return wall-clock seconds since state creation."""
        return time.time() - self.started_at

    def remaining_budget_seconds(self) -> float:
        """Return remaining wall-clock budget."""
        return max(0.0, self.budget.max_wall_time_seconds - self.elapsed_seconds())

    def is_budget_exhausted(self) -> bool:
        """Check if wall-clock budget is exhausted."""
        return self.elapsed_seconds() >= self.budget.max_wall_time_seconds

    def is_review_cycle_exhausted(self) -> bool:
        """Check if review cycle count reached maximum."""
        return self.review_cycle_count >= self.budget.max_review_cycles
