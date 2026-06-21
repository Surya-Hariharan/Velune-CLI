"""Tests for velune.cognition.council_runner — CouncilRunner pipeline."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.council.debate import DebateConfig, DebateSession, ScoredProposal
from velune.cognition.council.messages import ChallengerMessage
from velune.cognition.council_runner import CouncilRunner, _format_diffs, _plan_to_text
from velune.cognition.state import ReviewDecision
from velune.core.types.task import TaskPlan, TaskStatus, TaskStep
from velune.orchestration.schemas import ExecutionStatus, OrchestrationRequest, OrchestrationResult


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_plan(n_steps: int = 1) -> TaskPlan:
    steps = [
        TaskStep(
            id=f"step_{i}",
            description=f"Step {i}",
            agent_role="coder",
            status=TaskStatus.PENDING,
            metadata={"target_files": ["foo.py"], "expected_outcome": "done"},
        )
        for i in range(n_steps)
    ]
    return TaskPlan(task_id="task-test", steps=steps)


def _make_diffs(n: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "file_path": f"file_{i}.py",
            "original": "",
            "proposed": f"# impl {i}",
            "is_new_file": True,
            "is_deletion": False,
        }
        for i in range(n)
    ]


def _make_factory(
    *,
    plan: TaskPlan | None = None,
    diffs: list[dict[str, Any]] | None = None,
    review_decisions: list[tuple[ReviewDecision, str]] | None = None,
    challenger_severity: float = 0.1,
    synthesizer_output: str = "Final synthesized response.",
) -> MagicMock:
    """Build a mock CouncilAgentFactory that yields pre-configured agents."""
    plan = plan or _make_plan()
    diffs = diffs or _make_diffs()
    review_decisions = review_decisions or [(ReviewDecision.APPROVE, "")]

    factory = MagicMock()

    # Planner
    planner = MagicMock()
    planner.generate_plan = AsyncMock(return_value=plan)
    factory.create_planner.return_value = planner

    # Coder — each call returns diffs (supports multiple revision cycles)
    coder = MagicMock()
    coder.generate_code = AsyncMock(return_value=diffs)
    factory.create_coder.return_value = coder

    # Reviewer — yields decisions in order, then repeats the last one.
    # Also calls state.set_reviewer_output() so review_cycle_count increments correctly.
    decision_iter = iter(review_decisions)
    last_decision = review_decisions[-1]

    async def _review(task: str, proposal: str, context: str, state: Any, **kwargs: Any) -> tuple[ReviewDecision, str]:
        try:
            d, n = next(decision_iter)
        except StopIteration:
            d, n = last_decision
        state.set_reviewer_output(d, n)
        return d, n

    reviewer = MagicMock()
    reviewer.review_proposal = _review
    factory.create_reviewer.return_value = reviewer

    # Challenger
    challenger = MagicMock()
    challenge_msg = ChallengerMessage(
        assumptions_challenged=["assumes input is clean"],
        failure_vectors=["empty list edge case"],
        severity_rating=challenger_severity,
    )
    challenger.challenge = AsyncMock(return_value=challenge_msg)
    factory.create_challenger.return_value = challenger

    # Synthesizer
    synthesizer = MagicMock()
    synthesizer.synthesize = AsyncMock(return_value=synthesizer_output)
    factory.create_synthesizer.return_value = synthesizer

    return factory


def _make_request(prompt: str = "write a hello world function") -> OrchestrationRequest:
    return OrchestrationRequest(prompt=prompt, workspace="/tmp/workspace", task_id="test-run-1")


def _tight_budget() -> CouncilExecutionBudget:
    return CouncilExecutionBudget(
        max_wall_time_seconds=300,
        max_tokens_per_agent=512,
        max_review_cycles=2,
        planner_timeout_seconds=60,
        coder_timeout_seconds=60,
        reviewer_timeout_seconds=60,
    )


# ── Happy path: APPROVE on first review ──────────────────────────────────────


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_completed_result(self) -> None:
        factory = _make_factory(review_decisions=[(ReviewDecision.APPROVE, "")])
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="repo context")

        assert isinstance(result, OrchestrationResult)
        assert result.success is True
        assert result.status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_output_equals_synthesizer_response(self) -> None:
        expected = "My synthesized answer."
        factory = _make_factory(
            review_decisions=[(ReviewDecision.APPROVE, "")],
            synthesizer_output=expected,
        )
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="")

        assert result.output == expected

    @pytest.mark.asyncio
    async def test_plan_steps_recorded(self) -> None:
        factory = _make_factory(
            plan=_make_plan(3),
            review_decisions=[(ReviewDecision.APPROVE, "")],
        )
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="")

        assert result.plan_steps == 3

    @pytest.mark.asyncio
    async def test_coder_called_once_on_first_approve(self) -> None:
        factory = _make_factory(review_decisions=[(ReviewDecision.APPROVE, "")])
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        await runner.run(_make_request(), context="")

        factory.create_coder.return_value.generate_code.assert_called_once()


# ── REVISE then APPROVE ───────────────────────────────────────────────────────


class TestReviseOnce:
    @pytest.mark.asyncio
    async def test_revise_then_approve_is_success(self) -> None:
        factory = _make_factory(
            review_decisions=[
                (ReviewDecision.REVISE, "Fix the edge case"),
                (ReviewDecision.APPROVE, ""),
            ]
        )
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="")

        assert result.success is True
        assert result.status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_coder_called_twice_on_one_revise(self) -> None:
        factory = _make_factory(
            review_decisions=[
                (ReviewDecision.REVISE, "Fix it"),
                (ReviewDecision.APPROVE, ""),
            ]
        )
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        await runner.run(_make_request(), context="")

        # Initial + 1 revision = 2 calls
        assert factory.create_coder.return_value.generate_code.call_count == 2

    @pytest.mark.asyncio
    async def test_review_cycles_counted_in_metadata(self) -> None:
        factory = _make_factory(
            review_decisions=[
                (ReviewDecision.REVISE, "Fix it"),
                (ReviewDecision.APPROVE, ""),
            ]
        )
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="")

        assert result.metadata.get("review_cycles") == 2


# ── REJECT (max cycles exhausted) ────────────────────────────────────────────


class TestRejectPath:
    @pytest.mark.asyncio
    async def test_reject_after_max_cycles(self) -> None:
        # Budget allows 1 review cycle. Reviewer always returns REVISE.
        # After 1 cycle the loop exits via is_review_cycle_exhausted() with
        # decision still REVISE — the runner escalates that to REJECT.
        budget = CouncilExecutionBudget(
            max_wall_time_seconds=300,
            max_tokens_per_agent=512,
            max_review_cycles=1,
            planner_timeout_seconds=60,
            coder_timeout_seconds=60,
            reviewer_timeout_seconds=60,
        )
        factory = _make_factory(
            review_decisions=[(ReviewDecision.REVISE, "Issues found — cannot resolve")]
        )
        runner = CouncilRunner(factory=factory, default_budget=budget)
        result = await runner.run(_make_request(), context="")

        assert result.success is False
        assert result.status == ExecutionStatus.FAILED

    @pytest.mark.asyncio
    async def test_reject_metadata_contains_decision(self) -> None:
        budget = CouncilExecutionBudget(
            max_wall_time_seconds=300,
            max_tokens_per_agent=512,
            max_review_cycles=1,
            planner_timeout_seconds=60,
            coder_timeout_seconds=60,
            reviewer_timeout_seconds=60,
        )
        factory = _make_factory(
            review_decisions=[(ReviewDecision.REVISE, "Fatal issue")]
        )
        runner = CouncilRunner(factory=factory, default_budget=budget)
        result = await runner.run(_make_request(), context="")

        assert result.metadata.get("review_decision") == ReviewDecision.REJECT.value


# ── Exception / failure isolation ────────────────────────────────────────────


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_planner_exception_returns_failed_result(self) -> None:
        factory = MagicMock()
        planner = MagicMock()
        planner.generate_plan = AsyncMock(side_effect=RuntimeError("planner boom"))
        factory.create_planner.return_value = planner

        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="")

        assert result.success is False
        assert result.status == ExecutionStatus.FAILED
        assert "planner boom" in (result.error or "")

    @pytest.mark.asyncio
    async def test_synthesizer_exception_returns_failed_result(self) -> None:
        factory = _make_factory(review_decisions=[(ReviewDecision.APPROVE, "")])
        factory.create_synthesizer.return_value.synthesize = AsyncMock(
            side_effect=RuntimeError("synthesizer boom")
        )
        runner = CouncilRunner(factory=factory, default_budget=_tight_budget())
        result = await runner.run(_make_request(), context="")

        assert result.success is False
        assert "synthesizer boom" in (result.error or "")


# ── DebateSession unit tests ──────────────────────────────────────────────────


class TestDebateSession:
    def _msg(self, severity: float = 0.1, vectors: list[str] | None = None) -> ChallengerMessage:
        return ChallengerMessage(
            assumptions_challenged=["assumption A"],
            failure_vectors=vectors or ["vector B"],
            severity_rating=severity,
        )

    def test_single_proposal_returns_one_entry(self) -> None:
        session = DebateSession(DebateConfig())
        scored = session.run(
            proposals=["proposal text"],
            challenger_reports=[self._msg()],
            reviewer_decision=ReviewDecision.APPROVE,
        )
        assert len(scored) == 1
        assert scored[0].rank == 0

    def test_approve_with_low_severity_high_score(self) -> None:
        session = DebateSession(DebateConfig())
        scored = session.run(
            proposals=["proposal"],
            challenger_reports=[self._msg(severity=0.0, vectors=[])],
            reviewer_decision=ReviewDecision.APPROVE,
        )
        assert scored[0].score > 0.8

    def test_reject_lowers_score_significantly(self) -> None:
        session = DebateSession(DebateConfig())
        scored = session.run(
            proposals=["proposal"],
            challenger_reports=[self._msg(severity=0.5)],
            reviewer_decision=ReviewDecision.REJECT,
        )
        assert scored[0].score < 0.5

    def test_audit_reports_include_challenger_and_reviewer(self) -> None:
        session = DebateSession(DebateConfig())
        scored = session.run(
            proposals=["proposal"],
            challenger_reports=[self._msg()],
            reviewer_decision=ReviewDecision.APPROVE,
            reviewer_notes="Looks good",
        )
        types = {r["type"] for r in scored[0].audit_reports}
        assert "challenger" in types
        assert "reviewer" in types

    def test_multiple_proposals_ranked_by_score(self) -> None:
        session = DebateSession(DebateConfig())
        high_severity_msg = self._msg(severity=0.9, vectors=["a", "b", "c", "d", "e"])
        low_severity_msg = self._msg(severity=0.0, vectors=[])
        scored = session.run(
            proposals=["bad proposal", "good proposal"],
            challenger_reports=[high_severity_msg, low_severity_msg],
            reviewer_decision=ReviewDecision.APPROVE,
        )
        assert scored[0].content == "good proposal"
        assert scored[0].rank == 0
        assert scored[1].rank == 1


# ── Helper function tests ─────────────────────────────────────────────────────


class TestHelpers:
    def test_format_diffs_empty(self) -> None:
        result = _format_diffs([])
        assert "no diffs" in result

    def test_format_diffs_new_file(self) -> None:
        diffs = [{"file_path": "foo.py", "proposed": "x = 1", "is_new_file": True}]
        result = _format_diffs(diffs)
        assert "foo.py" in result
        assert "NEW FILE" in result
        assert "x = 1" in result

    def test_format_diffs_modified(self) -> None:
        diffs = [{"file_path": "bar.py", "proposed": "y = 2", "is_new_file": False}]
        result = _format_diffs(diffs)
        assert "MODIFIED" in result

    def test_plan_to_text_lists_steps(self) -> None:
        plan = _make_plan(2)
        text = _plan_to_text(plan)
        assert "step_0" in text
        assert "step_1" in text

    def test_plan_to_text_none(self) -> None:
        assert _plan_to_text(None) == ""
