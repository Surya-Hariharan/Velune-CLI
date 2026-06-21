"""Council runner — orchestrates the full planner→coder→reviewer→debate→synthesizer pipeline."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.council.debate import DebateConfig, DebateSession
from velune.cognition.state import CouncilState, ReviewDecision
from velune.orchestration.schemas import ExecutionStatus, OrchestrationResult

if TYPE_CHECKING:
    from velune.cognition.council.factory import CouncilAgentFactory
    from velune.orchestration.schemas import OrchestrationRequest

logger = logging.getLogger("velune.cognition.council_runner")


class CouncilRunner:
    """End-to-end council execution.

    Lifecycle:
    1. Planner decomposes the task into a plan DAG.
    2. Coder generates implementation diffs.
    3. Reviewer approves or requests revisions (up to budget.max_review_cycles).
    4. Challenger performs adversarial analysis on the final proposal.
    5. DebateSession scores the proposal.
    6. Synthesizer compiles everything into a final response.
    """

    def __init__(
        self,
        factory: CouncilAgentFactory,
        default_budget: CouncilExecutionBudget | None = None,
    ) -> None:
        self.factory = factory
        self.default_budget = default_budget or CouncilExecutionBudget()

    async def run(
        self,
        request: OrchestrationRequest,
        context: str,
        budget: CouncilExecutionBudget | None = None,
    ) -> OrchestrationResult:
        """Execute the full council pipeline and return a typed result.

        Args:
            request: Typed orchestration request (prompt, workspace, etc.).
            context: Pre-assembled context string (repo snapshot, memory, etc.).
            budget: Optional per-run budget override; falls back to default_budget.

        Returns:
            OrchestrationResult with output, success flag, and metadata.
        """
        run_id = request.task_id or uuid.uuid4().hex
        effective_budget = budget or self.default_budget

        state = CouncilState(
            run_id=run_id,
            task=request.prompt,
            budget=effective_budget,
        )

        try:
            return await self._execute(run_id, request, context, state)
        except Exception as exc:
            logger.error("Council pipeline failed for run=%s: %s", run_id, exc)
            state.mark_complete(error=str(exc))
            return OrchestrationResult(
                run_id=run_id,
                task_id=run_id,
                success=False,
                status=ExecutionStatus.FAILED,
                error=str(exc),
            )

    # ── Internal pipeline ─────────────────────────────────────────────────────

    async def _execute(
        self,
        run_id: str,
        request: OrchestrationRequest,
        context: str,
        state: CouncilState,
    ) -> OrchestrationResult:
        task = request.prompt

        # ── 1. Planner ────────────────────────────────────────────────────────
        logger.info("[%s] Phase 1: Planner", run_id)
        planner = self.factory.create_planner(run_id)
        task_plan = await planner.generate_plan(task, context, state)
        plan_str = _plan_to_text(task_plan)

        # ── 2. Coder (initial generation) ─────────────────────────────────────
        logger.info("[%s] Phase 2: Coder (initial)", run_id)
        coder = self.factory.create_coder(run_id)
        diffs = await coder.generate_code(task, context, plan_str, state)
        proposal = _format_diffs(diffs)

        # ── 3. Reviewer loop ──────────────────────────────────────────────────
        logger.info(
            "[%s] Phase 3: Reviewer loop (max cycles=%d)", run_id, state.budget.max_review_cycles
        )
        reviewer = self.factory.create_reviewer(run_id)
        decision = ReviewDecision.REVISE
        review_notes = ""

        while decision == ReviewDecision.REVISE and not state.is_review_cycle_exhausted():
            decision, review_notes = await reviewer.review_proposal(task, proposal, context, state)
            if decision == ReviewDecision.REVISE:
                logger.info(
                    "[%s] Reviewer requested revision (cycle %d/%d)",
                    run_id,
                    state.review_cycle_count,
                    state.budget.max_review_cycles,
                )
                diffs = await coder.generate_code(
                    task, context, plan_str, state, reviewer_notes=review_notes
                )
                proposal = _format_diffs(diffs)

        # If cycles exhausted while still in REVISE, escalate to REJECT.
        if decision == ReviewDecision.REVISE and state.is_review_cycle_exhausted():
            logger.warning(
                "[%s] Review cycles exhausted in REVISE state → escalating to REJECT", run_id
            )
            decision = ReviewDecision.REJECT
            review_notes = review_notes or "Max review cycles exhausted without resolution."

        # ── 4. Challenger ─────────────────────────────────────────────────────
        logger.info("[%s] Phase 4: Challenger", run_id)
        challenger = self.factory.create_challenger(run_id)
        challenge_report = await challenger.challenge(task, proposal, context)

        # ── 5. Debate ─────────────────────────────────────────────────────────
        logger.info("[%s] Phase 5: Debate scoring", run_id)
        task_complexity = "simple" if len(task_plan.steps) <= 2 else "structural"
        session = DebateSession(DebateConfig())
        scored = session.run(
            proposals=[proposal],
            challenger_reports=[challenge_report],
            reviewer_decision=decision,
            reviewer_notes=review_notes,
            task_complexity=task_complexity,
        )

        top = scored[0] if scored else None
        audit_reports: list[dict[str, Any]] = top.audit_reports if top else []

        # winning_claims = approved plan steps (what the council achieved)
        winning_claims: list[str] = [f"[{s.id}] {s.description}" for s in task_plan.steps]

        # ── 6. Synthesizer ────────────────────────────────────────────────────
        logger.info("[%s] Phase 6: Synthesizer", run_id)
        synthesizer = self.factory.create_synthesizer(run_id)
        final_output = await synthesizer.synthesize(
            task=task,
            winning_claims=winning_claims,
            plan=plan_str,
            audit_reports=audit_reports,
            context=context,
        )

        state.set_synthesizer_output(final_output)
        state.mark_complete()

        success = decision != ReviewDecision.REJECT
        status = ExecutionStatus.COMPLETED if success else ExecutionStatus.FAILED

        logger.info(
            "[%s] Council complete: success=%s decision=%s cycles=%d",
            run_id,
            success,
            decision.value,
            state.review_cycle_count,
        )

        return OrchestrationResult(
            run_id=run_id,
            task_id=run_id,
            success=success,
            status=status,
            output=final_output,
            plan_steps=len(task_plan.steps),
            attempts=state.review_cycle_count + 1,
            metadata={
                "review_decision": decision.value,
                "review_cycles": state.review_cycle_count,
                "debate_score": top.score if top else 0.0,
                "elapsed_seconds": state.elapsed_seconds(),
            },
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _plan_to_text(plan: Any) -> str:
    """Convert a TaskPlan to a compact text representation."""
    if plan is None:
        return ""
    lines: list[str] = [f"Task: {plan.task_id}"]
    for step in getattr(plan, "steps", []):
        files = step.metadata.get("target_files", []) if hasattr(step, "metadata") else []
        lines.append(f"  [{step.id}] {step.description}" + (f" → {files}" if files else ""))
    return "\n".join(lines)


def _format_diffs(diffs: list[dict[str, Any]]) -> str:
    """Render diff dicts as a human-readable text block for reviewer/challenger."""
    if not diffs:
        return "(no diffs produced)"
    parts: list[str] = []
    for diff in diffs:
        fp = diff.get("file_path", "unknown")
        proposed = diff.get("proposed", "")
        is_new = diff.get("is_new_file", False)
        label = "NEW FILE" if is_new else "MODIFIED"
        parts.append(f"=== {fp} [{label}] ===\n{proposed}")
    return "\n\n".join(parts)
