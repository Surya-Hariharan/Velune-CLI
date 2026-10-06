"""Plan mode: write a plan, revise it, approve it explicitly, then execute within its scope."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.cli.handlers import plan_flow
from velune.permissions import ExecutionMode, PolicyState
from velune.permissions.boundary import Boundary
from velune.permissions.gate import ActionDeniedError, Approval, PermissionGate
from velune.permissions.plans import (
    DONE,
    EXECUTING,
    WAITING,
    PlanManager,
    is_approval,
    parse_files_affected,
    parse_status,
)
from velune.tools.base.tool import ToolCallContext, ToolPermission, authorize_and_execute
from velune.tools.filesystem.write import WriteFile
from velune.tools.plan import WritePlan

PLAN = """# Plan

## Objective
Fix token expiry.

## Files Affected

| Action | File |
|---|---|
| Modify | `src/auth.py` |
| Add | tests/test_auth.py |

## Execution Order
1. Edit src/auth.py

## Approval
Status: WAITING_FOR_USER_APPROVAL
"""


@pytest.mark.parametrize(
    "text",
    [
        "approve",
        "Approved",
        "yes, proceed",
        "go ahead",
        "execute the plan",
        "implement it",
        "Approve.",
        "yes go ahead please",
    ],
)
def test_explicit_approval_phrases(text):
    assert is_approval(text)


@pytest.mark.parametrize(
    "text",
    [
        "looks good",
        "okay",
        "I see",
        "interesting",
        "approve but change step 3",
        "don't modify config.py",
        "go ahead and add Windows tests too",
    ],
)
def test_acknowledgements_and_revisions_are_not_approval(text):
    assert not is_approval(text)


def test_files_affected_table_parsing():
    assert parse_files_affected(PLAN) == [("Modify", "src/auth.py"), ("Add", "tests/test_auth.py")]
    assert parse_status(PLAN) == WAITING


def _ctx(ws: Path, state: PolicyState, ask=None) -> ToolCallContext:
    return ToolCallContext(
        run_id="t",
        actor="t",
        workspace=ws,
        permissions={ToolPermission.FILESYSTEM_WRITE},
        gate=PermissionGate(state, Boundary(ws), ask=ask),
    )


def _plan_state(pm: PlanManager) -> PolicyState:
    state = PolicyState(mode=ExecutionMode.PLAN)
    pm.apply_to(state)
    return state


async def test_planning_writes_the_plan_but_nothing_else(tmp_path):
    pm = PlanManager(tmp_path)
    ctx = _ctx(tmp_path, _plan_state(pm))
    out = await authorize_and_execute(WritePlan(tmp_path), ctx, name="Fix Auth", content=PLAN)
    plan = tmp_path / ".velune" / "plans" / "fix-auth.md"
    assert plan.exists() and "wait for the user" in out

    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            WriteFile(tmp_path, confirm=False), ctx, file_path="src/auth.py", content="x"
        )
    assert not (tmp_path / "src" / "auth.py").exists()


async def test_rewriting_a_plan_resets_it_to_waiting(tmp_path):
    pm = PlanManager(tmp_path)
    ctx = _ctx(tmp_path, _plan_state(pm))
    approved_looking = PLAN.replace(WAITING, "EXECUTING")
    await authorize_and_execute(WritePlan(tmp_path), ctx, name="x", content=approved_looking)
    assert parse_status((tmp_path / ".velune/plans/x.md").read_text()) == WAITING


async def test_execution_before_approval_is_blocked_and_after_is_scoped(tmp_path):
    pm = PlanManager(tmp_path)
    await authorize_and_execute(
        WritePlan(tmp_path), _ctx(tmp_path, _plan_state(pm)), name="fix", content=PLAN
    )
    pm.record(tmp_path / ".velune/plans/fix.md")
    assert pm.waiting

    # Before approval: denied.
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            WriteFile(tmp_path, confirm=False),
            _ctx(tmp_path, _plan_state(pm)),
            file_path="src/auth.py",
            content="fixed",
        )

    pm.approve()
    assert pm.executing
    assert parse_status(pm.active.read_text()) == EXECUTING

    # After approval: the plan's files are allowed without asking...
    await authorize_and_execute(
        WriteFile(tmp_path, confirm=False),
        _ctx(tmp_path, _plan_state(pm)),
        file_path="src/auth.py",
        content="fixed",
    )
    assert (tmp_path / "src/auth.py").read_text() == "fixed"

    # ...but a file outside the plan is a scope change that asks.
    asked = []

    async def deny(tool_name, decision):
        asked.append(decision)
        return Approval.DENY

    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            WriteFile(tmp_path, confirm=False),
            _ctx(tmp_path, _plan_state(pm), ask=deny),
            file_path="src/config.py",
            content="x",
        )
    assert asked and "scope change" in asked[0].reason

    pm.finish("All tests pass.")
    text = pm.active.read_text()
    assert parse_status(text) == DONE and "All tests pass." in text


def _repl(ws: Path):
    buf = io.StringIO()
    services = {"runtime.workspace": str(ws)}
    repl = SimpleNamespace(
        console=Console(file=buf, width=120, color_system=None),
        container=SimpleNamespace(get=lambda k: services[k], has=lambda k: k in services),
        _execution_mode=ExecutionMode.PLAN,
    )
    repl.output = buf
    return repl


def test_turn_flow_new_plan_then_revision_then_approval(tmp_path):
    repl = _repl(tmp_path)

    # 1. A new task: instructions to plan with write_plan and stop.
    instruction = plan_flow.before_turn(repl, "add rate limiting")
    assert instruction and instruction.startswith("PLAN MODE.") and "write_plan" in instruction
    plans = tmp_path / ".velune" / "plans"
    plans.mkdir(parents=True)
    (plans / "rate-limit.md").write_text(PLAN)
    plan_flow.after_turn(repl, "Plan written.")
    out = repl.output.getvalue()
    assert "Plan created" in out and "Modify 1" in out and "approve" in out
    assert "Waiting for approval." in out

    # 2. Not an approval: it's a revision; the plan keeps waiting.
    instruction = plan_flow.before_turn(repl, "don't modify config.py")
    assert instruction.startswith("The user is REVISING the plan")
    assert repl._plan_manager.waiting

    # "looks good" is not approval either.
    assert plan_flow.before_turn(repl, "looks good").startswith("The user is REVISING")

    # 3. Explicit approval: executes.
    instruction = plan_flow.before_turn(repl, "approve")
    assert instruction.startswith("The user APPROVED the plan")
    assert repl._plan_manager.executing

    # 4. After the execution turn the plan is DONE with the results.
    plan_flow.after_turn(repl, "Changed src/auth.py; 12 tests pass.")
    assert parse_status((plans / "rate-limit.md").read_text()) == DONE
    assert "Plan complete" in repl.output.getvalue()


def test_outside_plan_mode_the_flow_is_inert(tmp_path):
    repl = _repl(tmp_path)
    repl._execution_mode = ExecutionMode.AUTO
    assert plan_flow.before_turn(repl, "approve") is None


def test_stale_plan_instructions_are_dropped():
    convo = [
        {"role": "system", "content": "PLAN MODE. Investigate..."},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "unrelated context"},
    ]
    plan_flow.drop_stale_instructions(convo)
    assert [m["content"] for m in convo] == ["hi", "unrelated context"]


def test_write_plan_is_in_the_real_registry(tmp_path):
    from velune.tools.subsystems import _create_tool_registry

    services = {"runtime.execution_executor": SimpleNamespace(sandbox=None)}
    container = SimpleNamespace(get=lambda k: services[k], has=lambda k: k in services)
    reg = _create_tool_registry(SimpleNamespace(workspace=tmp_path, container=container))
    assert "write_plan" in reg.list_tools()


def test_a_plan_printed_as_text_is_saved_by_velune(tmp_path):
    """Seen live: the model printed the plan instead of calling write_plan, so the
    approve/revise flow had no plan and 'approve' was treated as a new task."""
    repl = _repl(tmp_path)
    plan_flow.before_turn(repl, "fix add()")
    plan_flow.after_turn(repl, PLAN.replace("Fix token expiry.", "Fix the add function"))
    pm = repl._plan_manager
    assert pm.waiting and pm.active.name == "fix-the-add-function.md"
    assert "Plan created" in repl.output.getvalue()
    # ...and the next message is now correctly understood as approval.
    assert plan_flow.before_turn(repl, "approve").startswith("The user APPROVED")


def test_ordinary_answers_are_not_mistaken_for_plans(tmp_path):
    repl = _repl(tmp_path)
    plan_flow.before_turn(repl, "what does calc.py do?")
    plan_flow.after_turn(repl, "## Objective\ncalc.py adds numbers.")
    assert repl._plan_manager.active is None
