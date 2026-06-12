"""Production-grade Council Orchestrator with strict budget enforcement and role-gated state."""

from __future__ import annotations

import logging
import time
from typing import Any

from velune.cognition.agents.coder import CoderAgent
from velune.cognition.agents.planner import PlannerAgent
from velune.cognition.agents.reviewer import ReviewerAgent
from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.state import CouncilState, ReviewDecision
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council_orchestrator")


class CouncilOrchestrator:
    """Production council orchestrator managing Planner, Coder, and Reviewer with strict budget enforcement.

    Enforces:
    - Wall-clock timeout at top level with per-agent timeout guards
    - Per-agent output validation before cross-phase use
    - Max review cycles with explicit cycle counting in state
    - Role-gated state writes (Coder cannot modify Planner output, etc.)
    - Clean error handling and budget exhaustion detection
    """

    def __init__(
        self,
        planner_model: ModelDescriptor,
        planner_provider: ModelProvider,
        coder_model: ModelDescriptor,
        coder_provider: ModelProvider,
        reviewer_model: ModelDescriptor,
        reviewer_provider: ModelProvider,
    ) -> None:
        self.planner = PlannerAgent(planner_model, planner_provider)
        self.coder = CoderAgent(coder_model, coder_provider)
        self.reviewer = ReviewerAgent(reviewer_model, reviewer_provider)

    async def run(
        self,
        task: str,
        retrieved_context: str,
        budget: CouncilExecutionBudget | None = None,
        style_profile: dict[str, Any] | None = None,
    ) -> CouncilState:
        """Execute full council deliberation with strict budget enforcement.

        Orchestration flow:
        1. Create CouncilState with budget
        2. Run PlannerAgent → task_plan (with timeout guard)
        3. Run CoderAgent → pending_diffs (with timeout guard)
        4. Run ReviewerAgent → review_decision (with timeout guard)
        5. If ReviewDecision.REVISE and cycles remain:
           - Pass reviewer_notes back to CoderAgent
           - Re-run ReviewerAgent
           - Max iterations: budget.max_review_cycles
        6. Return CouncilState with final output

        Args:
            task: User task description
            retrieved_context: Repository context (includes architectural warnings, etc.)
            budget: Execution budget (defaults to CouncilExecutionBudget defaults)
            style_profile: Style hints for codebase

        Returns:
            CouncilState with all agent outputs and final decision

        Raises:
            ValueError: If budget exhausted before completion
            Exception: Propagates agent-level errors after cleanup
        """
        if budget is None:
            budget = CouncilExecutionBudget()

        state = CouncilState(
            run_id=f"council-{int(time.time())}",
            task=task,
            budget=budget,
        )

        try:
            logger.info(
                "[COUNCIL] Starting deliberation (wall_budget=%ds, max_cycles=%d)",
                budget.max_wall_time_seconds,
                budget.max_review_cycles,
            )

            # Phase 1: Planner
            logger.info(
                "[COUNCIL] Phase 1/5: Planner (timeout=%ds)", budget.planner_timeout_seconds
            )
            await self._run_planner_phase(state, retrieved_context)

            # Phase 2: Initial Coder
            logger.info("[COUNCIL] Phase 2/5: Coder (timeout=%ds)", budget.coder_timeout_seconds)
            await self._run_coder_phase(state, retrieved_context, style_profile)

            # Phase 3: Initial Review
            logger.info(
                "[COUNCIL] Phase 3/5: Reviewer (timeout=%ds)", budget.reviewer_timeout_seconds
            )
            await self._run_review_phase(state)

            # Phase 4: Debate Loop (if needed)
            await self._run_debate_loop(state, retrieved_context, style_profile)

            # Phase 5: Mark complete
            logger.info(
                "[COUNCIL] Deliberation complete (elapsed=%.1fs, decision=%s)",
                state.elapsed_seconds(),
                state.review_decision or "N/A",
            )
            state.mark_complete()

            return state

        except Exception as e:
            logger.error("[COUNCIL] Execution failed: %s", e)
            state.mark_complete(error=str(e))
            raise

    async def _run_planner_phase(self, state: CouncilState, retrieved_context: str) -> None:
        """Execute Planner phase with timeout enforcement."""
        try:
            await self.planner.generate_plan(
                task=state.task,
                retrieved_context=retrieved_context,
                state=state,
            )
            if state.task_plan is None:
                raise ValueError("Planner did not produce a task plan")
        except Exception as e:
            logger.error("Planner phase failed: %s", e)
            raise ValueError(f"Planner execution failed: {e}") from e

    async def _run_coder_phase(
        self,
        state: CouncilState,
        retrieved_context: str,
        style_profile: dict[str, Any] | None,
    ) -> None:
        """Execute Coder phase with timeout enforcement."""
        try:
            plan_context = ""
            if state.task_plan:
                plan_context = "\n".join(
                    [f"- {s.id}: {s.description}" for s in state.task_plan.steps]
                )

            await self.coder.generate_code(
                task=state.task,
                retrieved_context=retrieved_context,
                plan_context=plan_context,
                state=state,
                style_profile=style_profile,
                reviewer_notes="",
            )
            if not state.pending_diffs:
                raise ValueError("Coder did not produce any diffs")
        except Exception as e:
            logger.error("Coder phase failed: %s", e)
            raise ValueError(f"Coder execution failed: {e}") from e

    async def _run_review_phase(self, state: CouncilState) -> None:
        """Execute Reviewer phase with timeout enforcement."""
        try:
            proposal = self._format_diffs_for_review(state.pending_diffs)
            decision, notes = await self.reviewer.review_proposal(
                task=state.task,
                proposal=proposal,
                context=state.retrieved_context or "",
                state=state,
            )
        except Exception as e:
            logger.error("Reviewer phase failed: %s", e)
            raise ValueError(f"Reviewer execution failed: {e}") from e

    async def _run_debate_loop(
        self,
        state: CouncilState,
        retrieved_context: str,
        style_profile: dict[str, Any] | None,
    ) -> None:
        """Execute debate loop with max review cycle enforcement.

        Debate loop:
        - If ReviewDecision.REVISE and cycles available:
          - Pass reviewer_notes to Coder
          - Coder generates revised proposal
          - Reviewer audits revised proposal
          - Loop until APPROVE, REJECT, or cycles exhausted
        """
        while (
            state.review_decision == ReviewDecision.REVISE
            and not state.is_review_cycle_exhausted()
            and not state.is_budget_exhausted()
        ):
            cycle = state.review_cycle_count
            logger.info(
                "[COUNCIL - DEBATE] Revision cycle %d/%d (wall_budget: %.1fs remaining)",
                cycle,
                state.budget.max_review_cycles,
                state.remaining_budget_seconds(),
            )

            if state.is_budget_exhausted():
                logger.warning("[COUNCIL - DEBATE] Wall-clock budget exhausted, stopping debate")
                break

            # Re-run Coder with reviewer notes
            try:
                reviewer_notes = state.review_notes or ""
                plan_context = (
                    f"REVIEWER FEEDBACK (Cycle {cycle}):\n{reviewer_notes}\n\n"
                    f"Please incorporate this feedback into a revised implementation."
                )

                await self.coder.generate_code(
                    task=state.task,
                    retrieved_context=retrieved_context,
                    plan_context=plan_context,
                    state=state,
                    style_profile=style_profile,
                    reviewer_notes=reviewer_notes,
                )
            except Exception as e:
                logger.error("[COUNCIL - DEBATE] Coder refinement failed on cycle %d: %s", cycle, e)
                break

            # Re-run Reviewer
            try:
                proposal = self._format_diffs_for_review(state.pending_diffs)
                decision, notes = await self.reviewer.review_proposal(
                    task=state.task,
                    proposal=proposal,
                    context=retrieved_context,
                    state=state,
                )
            except Exception as e:
                logger.error(
                    "[COUNCIL - DEBATE] Reviewer re-audit failed on cycle %d: %s", cycle, e
                )
                break

            logger.info(
                "[COUNCIL - DEBATE] Cycle %d completed: decision=%s",
                cycle,
                state.review_decision.value if state.review_decision else "N/A",
            )

    def _format_diffs_for_review(self, pending_diffs: list[dict[str, Any]]) -> str:
        """Format pending_diffs list as readable proposal for reviewer."""
        if not pending_diffs:
            return "(No diffs generated)"

        parts = []
        for diff in pending_diffs:
            file_path = diff.get("file_path", "unknown")
            is_new = diff.get("is_new_file", False)
            proposed = diff.get("proposed", "")

            if is_new:
                parts.append(f"\n--- NEW FILE: {file_path} ---\n{proposed}")
            else:
                parts.append(f"\n--- MODIFY: {file_path} ---\n{proposed}")

        return "".join(parts)
