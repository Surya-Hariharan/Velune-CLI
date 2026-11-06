"""Phase 4D-3: execution budgets, idempotency, cancellation and partial-side-effect semantics.

Synthetic tools only. The "blocking" tool is an async fake gated on an event, so the tests can hold a
call mid-flight, cancel around it, and prove it is never interrupted. No real tool, shell, network or
file write happens.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_engine_tool_executor import (
    SECRET,
    ApprovePort,
    SpyTool,
    _call,
    _executor,
    _run,
)
from velune.orchestration.engine import ApprovalFailure, HeadlessApprovalPort
from velune.orchestration.engine.execcontrol import (
    BudgetLedger,
    BudgetRefusal,
    ExecutionBudget,
    InvocationState,
    RunControl,
)
from velune.orchestration.engine.toolexec import (
    ToolErrorCategory,
    ToolOutcome,
    ToolStatus,
)
from velune.permissions.boundary import Boundary
from velune.permissions.gate import ActionDeniedError, PermissionGate
from velune.permissions.policy import ExecutionMode, PolicyState
from velune.tools.base.tool import ToolPermission, ToolPermissionError

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "velune" / "orchestration" / "engine"


class GateTool(SpyTool):
    """Enters, then waits for ``release``. Records whether anything interrupted it."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.interrupted = False

    async def execute(self, **kwargs: Any) -> Any:
        self.runs += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.interrupted = True
            raise
        self.finished = True
        return self.result


class ClockTool(SpyTool):
    """Advances an injected clock while it runs, like a slow synchronous operation."""

    def __init__(self, clock: list[float], by: float, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock, self.by = clock, by

    async def execute(self, **kwargs: Any) -> Any:
        self.runs += 1
        self.clock[0] += self.by
        return self.result


class TokenTool(SpyTool):
    """Requests cancellation from inside the call, then returns normally."""

    def __init__(self, control: RunControl, **kw: Any) -> None:
        super().__init__(**kw)
        self.control = control

    async def execute(self, **kwargs: Any) -> Any:
        self.runs += 1
        self.control.token.cancel()
        return self.result


def _control(clock: Any = None, run_id: str = "run-1", **budget: Any) -> RunControl:
    kw = {"clock": clock} if clock is not None else {}
    return RunControl.create(run_id, ExecutionBudget(**budget), **kw)


async def _go(executor, registry, control, request_id="r1", **args):
    call, policy = _call(registry, "read_file", **args)
    return await executor.execute(
        call, policy, run_id=control.run_id, control=control, request_id=request_id
    )


# ── budget configuration ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"max_tool_calls": 0},
        {"max_tool_calls": -1},
        {"max_tool_calls": True},
        {"max_tool_calls": 1.5},
        {"max_argument_bytes": 0},
        {"max_result_bytes": -5},
        {"max_single_result_bytes": 0},
        {"max_result_bytes": 10, "max_single_result_bytes": 11},
        {"wall_clock_s": 0.0},
        {"wall_clock_s": -1.0},
        {"wall_clock_s": float("nan")},
        {"wall_clock_s": float("inf")},
        {"max_concurrent": 0},
        {"max_concurrent": 999},
        {"unknown_limit": 1},
    ],
)
def test_invalid_budgets_are_rejected(bad):
    with pytest.raises(ValidationError):
        ExecutionBudget(**bad)


def test_budget_is_immutable_and_concurrency_defaults_to_one():
    budget = ExecutionBudget()
    assert budget.max_concurrent == 1
    with pytest.raises(ValidationError):
        budget.max_tool_calls = 99  # type: ignore[misc]


# ── ledger: deterministic, atomic, monotonic ──────────────────────────────────


def test_ledger_counters_are_monotonic_and_deterministic():
    now = [100.0]
    ledger = BudgetLedger(ExecutionBudget(max_tool_calls=5, wall_clock_s=50.0), lambda: now[0])
    seen = [ledger.usage()]
    for size in (10, 20, 5):
        res = ledger.reserve(size)
        assert not isinstance(res, BudgetRefusal)
        now[0] += 1.0
        res.settle(size * 2)
        res.settle(9999)  # settling twice is a no-op
        seen.append(ledger.usage())
    for before, after in zip(seen, seen[1:], strict=False):
        assert after.calls >= before.calls
        assert after.argument_bytes >= before.argument_bytes
        assert after.result_bytes >= before.result_bytes
    last = seen[-1]
    assert (last.calls, last.argument_bytes, last.result_bytes, last.in_flight) == (3, 35, 70, 0)
    assert last.elapsed_s == 3.0


def test_ledger_reservation_is_atomic_against_the_limits():
    ledger = BudgetLedger(ExecutionBudget(max_tool_calls=1, max_concurrent=4))
    first = ledger.reserve(1)
    assert not isinstance(first, BudgetRefusal)
    assert ledger.reserve(1) is BudgetRefusal.CALLS
    first.release()
    assert ledger.reserve(1) is BudgetRefusal.CALLS  # a finished call is never un-counted


def test_ledger_deadline_uses_the_injected_clock():
    now = [0.0]
    ledger = BudgetLedger(ExecutionBudget(wall_clock_s=5.0), lambda: now[0])
    assert ledger.check(1) is None and not ledger.past_deadline()
    now[0] = 5.0
    assert ledger.check(1) is BudgetRefusal.DEADLINE and ledger.past_deadline()


def test_two_runs_share_no_accounting():
    a, b = _control(run_id="run-a", max_tool_calls=1), _control(run_id="run-b", max_tool_calls=1)
    assert not isinstance(a.budget.reserve(1), BudgetRefusal)
    assert a.budget.reserve(1) is BudgetRefusal.CALLS
    assert not isinstance(b.budget.reserve(1), BudgetRefusal)


# ── budgets through the executor ──────────────────────────────────────────────


async def test_call_count_limit(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(max_tool_calls=2)
    results = [await _go(executor, registry, control, f"r{i}") for i in range(3)]
    assert [r.ok for r in results] == [True, True, False]
    assert results[2].error is ToolErrorCategory.BUDGET_CALLS
    assert results[2].outcome is ToolOutcome.FAILED_BEFORE_EXECUTION and not results[2].executed
    assert tool.runs == 2


async def test_argument_byte_limit(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    call, _ = _call(registry)
    size = len(call.arguments_json.encode())
    control = _control(max_argument_bytes=size + 3)
    assert (await _go(executor, registry, control, "r1")).ok
    second = await _go(executor, registry, control, "r2")
    assert second.error is ToolErrorCategory.BUDGET_ARGUMENT_BYTES and tool.runs == 1
    assert control.budget.usage().argument_bytes == size


async def test_result_byte_limits_total_and_individual(tmp_path):
    tool = SpyTool(result="x" * 100)
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(max_single_result_bytes=100, max_result_bytes=150)
    first = await _go(executor, registry, control, "r1")
    second = await _go(executor, registry, control, "r2")
    third = await _go(executor, registry, control, "r3")
    assert (len(first.output), first.truncated) == (100, False)
    assert (len(second.output), second.truncated) == (50, True)  # clipped to what is left
    assert third.error is ToolErrorCategory.BUDGET_RESULT_BYTES and tool.runs == 2
    assert control.budget.usage().result_bytes == 150


async def test_individual_result_limit_clips_multibyte_text_safely(tmp_path):
    tool = SpyTool(result="é" * 50)  # 2 bytes each
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(max_single_result_bytes=11, max_result_bytes=100)
    result = await _go(executor, registry, control)
    assert result.truncated and len(result.output.encode()) <= 11
    assert control.budget.usage().result_bytes == len(result.output.encode())


async def test_wall_clock_deadline_before_execution(tmp_path):
    now = [0.0]
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(clock=lambda: now[0], wall_clock_s=10.0)
    assert (await _go(executor, registry, control, "r1")).ok
    now[0] = 10.0
    late = await _go(executor, registry, control, "r2")
    assert late.error is ToolErrorCategory.BUDGET_DEADLINE and tool.runs == 1


async def test_failed_invocation_counts_once_and_denied_approval_does_not(tmp_path):
    failing = SpyTool(raises=RuntimeError("boom"))
    registry, executor = _executor(tmp_path, failing, ApprovePort())
    control = _control()
    assert (await _go(executor, registry, control, "r1")).error is ToolErrorCategory.TOOL_ERROR
    assert control.budget.usage().calls == 1

    registry2, denier = _executor(tmp_path, SpyTool(), HeadlessApprovalPort())
    control2 = _control()
    denied = await _go(denier, registry2, control2, "r1")
    assert denied.error is ToolErrorCategory.APPROVAL_DENIED
    assert control2.budget.usage().calls == 0 and control2.budget.usage().argument_bytes == 0


async def test_authorization_denial_still_counts_as_a_started_attempt(tmp_path):
    tool = SpyTool(outside=True)  # the gate refuses it, but the attempt entered the execution path
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    result = await _go(executor, registry, control)
    assert result.error is ToolErrorCategory.AUTHORIZATION_DENIED and tool.runs == 0
    assert result.outcome is ToolOutcome.FAILED_BEFORE_EXECUTION and not result.executed
    assert control.budget.usage().calls == 1


async def test_retry_under_a_new_request_id_does_not_reset_the_budget(tmp_path):
    tool = SpyTool(raises=RuntimeError("boom"))
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(max_tool_calls=2)
    await _go(executor, registry, control, "r1")
    await _go(executor, registry, control, "r1-retry")
    third = await _go(executor, registry, control, "r1-retry-2")
    assert third.error is ToolErrorCategory.BUDGET_CALLS and tool.runs == 2


async def test_concurrent_calls_cannot_exceed_the_call_limit(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(max_tool_calls=1, max_concurrent=4)
    results = await asyncio.gather(*(_go(executor, registry, control, f"r{i}") for i in range(4)))
    assert sum(r.executed for r in results) == 1 and tool.runs == 1
    assert {r.error for r in results if not r.executed} == {ToolErrorCategory.BUDGET_CALLS}
    assert control.budget.usage().calls == 1


async def test_default_concurrency_of_one_serialises_calls(tmp_path):
    tool = GateTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    first = asyncio.create_task(_go(executor, registry, control, "r1"))
    await tool.entered.wait()
    second = await _go(executor, registry, control, "r2", file_path="b.txt")
    assert second.error is ToolErrorCategory.BUDGET_CONCURRENCY and second.safe_to_retry
    assert tool.runs == 1
    tool.release.set()
    assert (await first).ok
    assert control.budget.usage().in_flight == 0


# ── idempotency ───────────────────────────────────────────────────────────────


async def test_same_request_executes_at_most_once_and_is_replayed(tmp_path):
    tool = SpyTool(result="payload")
    port = ApprovePort()
    registry, executor = _executor(tmp_path, tool, port)
    control = _control()
    first = await _go(executor, registry, control, "r1")
    again = await _go(executor, registry, control, "r1")
    assert tool.runs == 1 and len(port.seen) == 1  # no second approval either
    assert again.replayed and again.output == first.output == "payload" and again.ok
    assert control.budget.usage().calls == 1
    assert control.store.state_of("r1") is InvocationState.COMPLETED


async def test_duplicate_of_a_pending_call_is_rejected(tmp_path):
    tool = GateTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(max_concurrent=2)
    first = asyncio.create_task(_go(executor, registry, control, "r1"))
    await tool.entered.wait()
    dup = await _go(executor, registry, control, "r1")
    assert dup.error is ToolErrorCategory.DUPLICATE_PENDING and not dup.safe_to_retry
    assert tool.runs == 1
    tool.release.set()
    assert (await first).ok


async def test_same_request_id_with_different_arguments_is_rejected(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    assert (await _go(executor, registry, control, "r1", file_path="a.txt")).ok
    clash = await _go(executor, registry, control, "r1", file_path="other.txt")
    assert clash.error is ToolErrorCategory.IDEMPOTENCY_CONFLICT and not clash.safe_to_retry
    assert tool.runs == 1


async def test_failed_call_is_recorded_and_never_rerun(tmp_path):
    tool = SpyTool(raises=RuntimeError(f"boom {SECRET}"))
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    first = await _go(executor, registry, control, "r1")
    again = await _go(executor, registry, control, "r1")
    assert tool.runs == 1 and again.replayed and not again.safe_to_retry
    assert again.outcome is ToolOutcome.FAILED_DURING_EXECUTION
    assert control.store.state_of("r1") is InvocationState.FAILED
    assert SECRET not in first.model_dump_json() + again.model_dump_json()


async def test_unknown_outcome_is_recorded_and_never_rerun(tmp_path):
    tool = GateTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    task = asyncio.create_task(_go(executor, registry, control, "r1"))
    await tool.entered.wait()
    control.token.cancel()
    first = await task
    again = await _go(executor, registry, control, "r1")
    assert tool.runs == 1 and again.replayed and not again.safe_to_retry
    assert again.outcome is ToolOutcome.CANCEL_REQUESTED_DURING_EXECUTION
    tool.release.set()
    await control.drain()
    assert first.outcome is ToolOutcome.CANCEL_REQUESTED_DURING_EXECUTION
    assert control.store.state_of("r1") is InvocationState.UNKNOWN  # a late finish changes nothing


async def test_idempotency_is_isolated_across_runs(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    run_a, run_b = _control(run_id="run-a"), _control(run_id="run-b")
    assert (await _go(executor, registry, run_a, "r1")).ok
    second = await _go(executor, registry, run_b, "r1")
    assert second.ok and not second.replayed and tool.runs == 2


async def test_refusal_before_start_leaves_the_request_retryable(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, HeadlessApprovalPort())
    control = _control()
    denied = await _go(executor, registry, control, "r1")
    assert denied.safe_to_retry and control.store.state_of("r1") is None
    approving = TrustedExecutorFor(tmp_path, registry)
    ok = await _go(approving, registry, control, "r1")
    assert ok.ok and tool.runs == 1  # the caller decided to retry; nothing retried by itself


def TrustedExecutorFor(tmp_path, registry):  # noqa: N802 - small local factory
    from velune.orchestration.engine.toolexec import TrustedToolExecutor

    return TrustedToolExecutor(registry, tmp_path, ApprovePort())


async def test_untrusted_request_ids_and_run_mismatch_are_malformed(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    call, policy = _call(registry)
    for rid in (None, "", "bad id", "x" * 80, "../etc"):
        res = await executor.execute(call, policy, run_id="run-1", control=control, request_id=rid)
        assert res.error is ToolErrorCategory.MALFORMED
    other = await executor.execute(call, policy, run_id="other", control=control, request_id="r1")
    assert other.error is ToolErrorCategory.MALFORMED and tool.runs == 0


# ── cancellation ──────────────────────────────────────────────────────────────


async def test_cancellation_before_execution_prevents_the_call(tmp_path):
    tool = SpyTool()
    port = ApprovePort()
    registry, executor = _executor(tmp_path, tool, port)
    control = _control()
    control.token.cancel()
    result = await _go(executor, registry, control)
    assert result.outcome is ToolOutcome.CANCELLED_BEFORE_EXECUTION and not result.executed
    assert tool.runs == 0 and port.seen == [] and control.budget.usage().calls == 0
    assert control.store.state_of("r1") is InvocationState.CANCELLED
    assert (await _go(executor, registry, control)).replayed


async def test_cancellation_after_approval_keeps_the_approval_spent(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool)
    control = _control()
    decisions = []

    class CancellingPort(ApprovePort):
        async def request(self, invocation):
            decision = await super().request(invocation)
            decisions.append(decision)
            control.token.cancel()  # cancelled while the approver was answering
            return decision

    executor._approvals = CancellingPort()
    result = await _go(executor, registry, control)
    assert result.outcome is ToolOutcome.CANCELLED_BEFORE_EXECUTION and tool.runs == 0
    assert control.budget.usage().calls == 0
    assert executor._ledger.consume(decisions[0]) is ApprovalFailure.STALE  # never restored
    assert executor._ledger.pending_count == 0


async def test_cancellation_during_a_running_call_reports_uncertainty_and_does_not_interrupt(
    tmp_path,
):
    tool = GateTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    task = asyncio.create_task(_go(executor, registry, control))
    await tool.entered.wait()
    control.token.cancel()
    result = await task
    assert result.outcome is ToolOutcome.CANCEL_REQUESTED_DURING_EXECUTION
    assert result.executed and result.cancel_requested and not result.ok
    assert not result.safe_to_retry and result.error is ToolErrorCategory.OUTCOME_UNKNOWN
    # Not interrupted, not finished: it is still running and still holds its slot.
    assert not tool.interrupted and not tool.finished
    assert control.abandoned_count == 1 and control.budget.usage().in_flight == 1
    tool.release.set()
    await control.drain()
    assert tool.finished and not tool.interrupted
    usage = control.budget.usage()
    assert (usage.calls, usage.in_flight, usage.result_bytes) == (1, 0, 0)  # late output discarded


async def test_outer_asyncio_cancellation_leaves_the_tool_running_and_records_unknown(tmp_path):
    tool = GateTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    task = asyncio.create_task(_go(executor, registry, control))
    await tool.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert control.store.state_of("r1") is InvocationState.UNKNOWN
    assert not tool.interrupted and not tool.finished
    assert control.budget.usage().calls == 1
    again = await _go(executor, registry, control)
    assert again.replayed and again.outcome is ToolOutcome.OUTCOME_UNKNOWN and tool.runs == 1
    tool.release.set()
    await control.drain()
    assert tool.finished and not tool.interrupted and control.budget.usage().in_flight == 0


async def test_cancellation_after_completion_preserves_the_result(tmp_path):
    tool = SpyTool(result="done")
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    first = await _go(executor, registry, control)
    control.token.cancel()
    again = await _go(executor, registry, control)
    assert first.ok and again.ok and again.replayed and again.output == "done"
    assert control.store.state_of("r1") is InvocationState.COMPLETED


async def test_cancel_requested_but_call_finished_keeps_its_result(tmp_path):
    control = _control()
    tool = TokenTool(control, result="kept")
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    result = await _go(executor, registry, control)
    assert result.ok and result.output == "kept" and result.cancel_requested
    assert result.outcome is ToolOutcome.COMPLETED


async def test_cancelled_run_cannot_start_new_calls_and_budget_stays_accounted(tmp_path):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    assert (await _go(executor, registry, control, "r1")).ok
    control.token.cancel()
    blocked = await _go(executor, registry, control, "r2")
    assert blocked.outcome is ToolOutcome.CANCELLED_BEFORE_EXECUTION
    assert tool.runs == 1 and control.budget.usage().calls == 1


# ── partial side effects ──────────────────────────────────────────────────────


async def test_exception_after_entry_is_conservative_and_never_retried(tmp_path):
    tool = SpyTool(raises=ValueError(f"/secret/path {SECRET}"))
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    result = await _go(executor, registry, control)
    assert result.outcome is ToolOutcome.FAILED_DURING_EXECUTION and result.executed
    assert result.status is ToolStatus.FAILED and not result.safe_to_retry
    assert SECRET not in result.model_dump_json() and "/secret/path" not in result.model_dump_json()
    assert tool.runs == 1  # nothing retried on its own


async def test_deadline_exceeded_after_synchronous_return_is_reported_accurately(tmp_path):
    now = [0.0]
    tool = ClockTool(now, by=100.0, result="late but real")
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control(clock=lambda: now[0], wall_clock_s=10.0)
    result = await _go(executor, registry, control)
    assert result.outcome is ToolOutcome.DEADLINE_EXCEEDED and result.error is (
        ToolErrorCategory.DEADLINE_EXCEEDED
    )
    assert result.executed and not result.ok and not result.safe_to_retry
    assert result.output == "late but real"  # it ran and returned; its effects are not undone
    assert control.store.state_of("r1") is InvocationState.COMPLETED
    assert control.budget.usage().result_bytes == len(b"late but real")
    again = await _go(executor, registry, control)
    assert again.replayed and tool.runs == 1


async def test_no_executed_outcome_is_ever_marked_safe_to_retry(tmp_path):
    cases = []
    control = _control(max_concurrent=4)
    for i, tool in enumerate((SpyTool(raises=RuntimeError("x")), SpyTool(result="ok"))):
        registry, executor = _executor(tmp_path, tool, ApprovePort())
        cases.append(await _go(executor, registry, control, f"r{i}"))
    gate = GateTool()
    registry, executor = _executor(tmp_path, gate, ApprovePort())
    task = asyncio.create_task(_go(executor, registry, control, "rg"))
    await gate.entered.wait()
    control.token.cancel()
    cases.append(await task)
    gate.release.set()
    await control.drain()
    executed = [c for c in cases if c.executed]
    assert executed and not any(c.safe_to_retry for c in executed)


async def test_unexpected_internal_error_after_start_is_unknown_not_retryable(
    tmp_path, monkeypatch
):
    import velune.orchestration.engine.toolexec as toolexec

    def explode(raw):
        raise RuntimeError("bug")

    monkeypatch.setattr(toolexec, "bound_output", explode)
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    control = _control()
    result = await _go(executor, registry, control)
    assert result.error is ToolErrorCategory.INTERNAL
    assert result.outcome is ToolOutcome.OUTCOME_UNKNOWN and result.executed
    assert not result.safe_to_retry
    assert control.store.state_of("r1") is InvocationState.UNKNOWN
    assert control.budget.usage().in_flight == 0


# ── structural guarantees ─────────────────────────────────────────────────────


def test_no_unsafe_interruption_in_the_source():
    for name in ("toolexec.py", "execcontrol.py"):
        tree = ast.parse((ENGINE / name).read_text(encoding="utf-8"))
        cancel_targets = [
            n.func.value.id if isinstance(n.func.value, ast.Name) else "?"
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "cancel"
        ]
        # The only things ever cancelled are our own helper ("waiter") and the token flag.
        assert set(cancel_targets) <= {"waiter"} | ({"self"} if name == "execcontrol.py" else set())
        text = (ENGINE / name).read_text(encoding="utf-8")
        for banned in ("kill(", "terminate(", "threading.Thread", "ctypes", "wait_for(", "signal."):
            assert banned not in text, (name, banned)


def test_execcontrol_is_pure_bookkeeping():
    tree = ast.parse((ENGINE / "execcontrol.py").read_text(encoding="utf-8"))
    imported = " ".join(
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    )
    for banned in ("velune.tools", "velune.permissions", "velune.cli", "velune.providers"):
        assert banned not in imported


def test_toolexec_is_still_not_exported_from_the_package():
    text = (ENGINE / "__init__.py").read_text(encoding="utf-8")
    assert "toolexec" not in text and "execcontrol" not in text


# ── conservative execution classification (Phase 4D-3 review fix) ────────────
#
# Exception type never proves a tool did not run: a tool body may raise ActionDeniedError or
# ToolPermissionError itself. Only a denial raised before the executor's gate check returned (so
# before guarded_execute and the body) is provably pre-execution.


def _body_errors():
    return [
        ActionDeniedError("read_file", f"body says no {SECRET}"),
        ToolPermissionError("read_file", {ToolPermission.FILESYSTEM_READ}),
        RuntimeError(f"ordinary {SECRET}"),
    ]


@pytest.mark.parametrize("error", _body_errors(), ids=["action_denied", "tool_permission", "plain"])
async def test_an_error_raised_by_the_tool_body_is_possibly_executed_whatever_its_type(
    tmp_path, error
):
    tool = SpyTool(raises=error)
    port = ApprovePort()
    registry, executor = _executor(tmp_path, tool, port)
    control = _control()

    first = await _go(executor, registry, control)
    assert tool.runs == 1  # the body was entered
    assert first.status is ToolStatus.FAILED and first.error is ToolErrorCategory.TOOL_ERROR
    assert first.outcome is ToolOutcome.FAILED_DURING_EXECUTION
    assert first.executed and not first.safe_to_retry
    assert SECRET not in first.model_dump_json() and "body says no" not in first.model_dump_json()
    assert control.store.state_of("r1") is InvocationState.FAILED
    assert control.budget.usage().calls == 1

    # Idempotency: the same request is replayed, never run again, and costs nothing more.
    again = await _go(executor, registry, control)
    assert again.replayed and again.executed and not again.safe_to_retry
    assert again.outcome is ToolOutcome.FAILED_DURING_EXECUTION
    assert tool.runs == 1 and len(port.seen) == 1
    assert control.budget.usage().calls == 1
    assert executor._ledger.pending_count == 0  # the spent approval was not restored


@pytest.mark.parametrize("error", _body_errors(), ids=["action_denied", "tool_permission", "plain"])
async def test_the_uncontrolled_path_is_equally_conservative(tmp_path, error):
    tool = SpyTool(raises=error)
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    result, *_ = await _run(executor, registry)
    assert tool.runs == 1
    assert result.status is ToolStatus.FAILED and result.error is ToolErrorCategory.TOOL_ERROR
    assert result.executed and not result.safe_to_retry
    assert result.outcome is ToolOutcome.FAILED_DURING_EXECUTION


async def test_a_gate_denial_before_the_body_is_provably_unexecuted(tmp_path):
    tool = SpyTool(outside=True)  # describe_actions targets outside the workspace: the gate refuses
    port = ApprovePort()
    registry, executor = _executor(tmp_path, tool, port)
    control = _control()

    result = await _go(executor, registry, control)
    assert tool.runs == 0  # the body was not entered
    assert result.status is ToolStatus.REFUSED
    assert result.error is ToolErrorCategory.AUTHORIZATION_DENIED
    assert result.outcome is ToolOutcome.FAILED_BEFORE_EXECUTION
    assert not result.executed and result.safe_to_retry
    # No automatic retry; the attempt is counted; the approval stays spent; the claim is released.
    assert len(port.seen) == 1 and control.budget.usage().calls == 1
    assert executor._ledger.pending_count == 0
    assert control.store.state_of("r1") is None


async def test_a_missing_scope_is_provably_unexecuted(tmp_path, monkeypatch):
    tool = SpyTool()
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    real_context = executor._context
    monkeypatch.setattr(
        executor,
        "_context",
        lambda t, run_id: dataclasses.replace(real_context(t, run_id), permissions=set()),
    )
    result = await _go(executor, registry, _control())
    assert tool.runs == 0 and result.error is ToolErrorCategory.AUTHORIZATION_DENIED
    assert not result.executed and result.safe_to_retry


async def test_a_denial_that_cannot_be_proven_pre_entry_is_treated_as_possibly_executed(
    tmp_path, monkeypatch
):
    # A gate that is not the executor's own probe: whether its check returned is unknowable, so even
    # a genuine denial from it must not be reported as "never began".
    tool = SpyTool(outside=True)
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    real_context = executor._context

    def foreign_gate_context(t, run_id):
        ctx = real_context(t, run_id)
        plain = PermissionGate(
            state=PolicyState(mode=ExecutionMode.MANUAL), boundary=Boundary(tmp_path), ask=None
        )
        return dataclasses.replace(ctx, gate=plain)

    monkeypatch.setattr(executor, "_context", foreign_gate_context)
    control = _control()
    result = await _go(executor, registry, control)
    assert tool.runs == 0  # (in fact it did not run, but the executor cannot prove that)
    assert result.executed and not result.safe_to_retry
    assert result.outcome is ToolOutcome.FAILED_DURING_EXECUTION
    assert result.error is ToolErrorCategory.TOOL_ERROR
    assert control.store.state_of("r1") is InvocationState.FAILED
    assert control.budget.usage().calls == 1


async def test_a_tool_that_raises_after_the_gate_passed_cannot_masquerade_as_a_gate_denial(
    tmp_path,
):
    # The strongest shape of the bug: the body raises the exact error the gate raises.
    tool = SpyTool(raises=ActionDeniedError("read_file", "no permission gate is active"))
    registry, executor = _executor(tmp_path, tool, ApprovePort())
    result = await _go(executor, registry, _control())
    assert tool.runs == 1 and result.executed and not result.safe_to_retry
    assert result.status is not ToolStatus.REFUSED
