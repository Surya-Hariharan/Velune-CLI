"""Execution modes: plan approval can't be bypassed, AUTO is bounded, pending state is explicit."""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from velune.cli.execution_modes import (
    BADGES,
    cycle_execution_mode,
    mode_badge,
    set_execution_mode,
)
from velune.cli.handlers import plan_flow
from velune.cli.statusbar import StatusBarState, render_status_bar
from velune.permissions import ExecutionMode, PolicyState, Verdict, authorize
from velune.permissions.actions import Action, ActionType
from velune.permissions.boundary import Boundary, command_paths_outside
from velune.permissions.plans import EXECUTING, WAITING, PlanManager, parse_status

PLAN = """# Plan

## Objective
Fix token expiry.

## Files Affected

| Action | File |
|---|---|
| Modify | `src/auth.py` |

## Execution Order
1. Edit src/auth.py

## Approval
Status: WAITING_FOR_USER_APPROVAL
"""


def _repl(ws: Path, mode: ExecutionMode = ExecutionMode.PLAN):
    buf = io.StringIO()
    services = {"runtime.workspace": str(ws)}
    state = PolicyState(mode=mode)
    repl = SimpleNamespace(
        console=Console(file=buf, width=120, color_system=None),
        container=SimpleNamespace(get=lambda k: services[k], has=lambda k: k in services),
        _execution_mode=mode,
        _status_state=StatusBarState(),
        _permission_gate=SimpleNamespace(state=state, boundary=Boundary(ws)),
    )
    repl.output = buf
    return repl


def _waiting_plan(repl, ws: Path) -> PlanManager:
    plans = ws / ".velune" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "fix.md").write_text(PLAN, encoding="utf-8")
    plan_flow.before_turn(repl, "fix the token expiry")
    plan_flow.after_turn(repl, "Plan written.")
    return repl._plan_manager


def _edit() -> Action:
    return Action(ActionType.MODIFY_FILE, "src/auth.py")


# ── plan approval cannot be bypassed by switching modes ─────────────────────


def test_leaving_plan_mode_pauses_an_executing_plan(tmp_path):
    repl = _repl(tmp_path)
    pm = _waiting_plan(repl, tmp_path)
    plan_flow.before_turn(repl, "approve")
    assert pm.executing

    set_execution_mode(repl, ExecutionMode.AUTO)

    assert not pm.executing and pm.waiting
    assert parse_status(pm.active.read_text(encoding="utf-8")) == WAITING
    assert "paused" in repl.output.getvalue()


def test_returning_to_plan_does_not_resume_without_a_new_approval(tmp_path):
    repl = _repl(tmp_path)
    pm = _waiting_plan(repl, tmp_path)
    plan_flow.before_turn(repl, "approve")
    set_execution_mode(repl, ExecutionMode.MANUAL)
    set_execution_mode(repl, ExecutionMode.PLAN)

    state = repl._permission_gate.state
    assert state.plan_executing is False
    assert authorize([_edit()], state).verdict is Verdict.DENY

    plan_flow.before_turn(repl, "approve")  # the explicit re-approval
    pm.apply_to(state)
    assert state.plan_executing is True


def test_a_stale_executing_plan_cannot_unlock_the_next_planning_turn(tmp_path):
    repl = _repl(tmp_path)
    pm = _waiting_plan(repl, tmp_path)
    plan_flow.before_turn(repl, "approve")
    set_execution_mode(repl, ExecutionMode.AUTO)
    set_execution_mode(repl, ExecutionMode.PLAN)

    instruction = plan_flow.before_turn(repl, "now plan something else")
    assert instruction is not None and not pm.executing
    pm.apply_to(repl._permission_gate.state)
    assert authorize([_edit()], repl._permission_gate.state).verdict is Verdict.DENY


def test_approving_outside_plan_mode_does_nothing(tmp_path):
    repl = _repl(tmp_path)
    pm = _waiting_plan(repl, tmp_path)
    set_execution_mode(repl, ExecutionMode.AUTO)

    assert plan_flow.before_turn(repl, "approve") is None
    assert pm.waiting and not pm.executing


def test_a_status_written_by_the_model_is_never_trusted(tmp_path):
    repl = _repl(tmp_path)
    plans = tmp_path / ".velune" / "plans"
    plans.mkdir(parents=True)
    plan_flow.before_turn(repl, "plan it")
    forged = plans / "forged.md"
    forged.write_text(PLAN.replace(WAITING, EXECUTING), encoding="utf-8")

    plan_flow.after_turn(repl, "Plan written.")

    pm = repl._plan_manager
    assert pm.waiting and not pm.executing
    assert parse_status(forged.read_text(encoding="utf-8")) == WAITING


def test_suspend_is_a_noop_when_nothing_executes(tmp_path):
    assert PlanManager(tmp_path).suspend() is False


# ── pending plan state is explicit ──────────────────────────────────────────


def test_badge_names_the_pending_plan_state(tmp_path):
    repl = _repl(tmp_path)
    assert mode_badge(repl) == BADGES[ExecutionMode.PLAN]

    _waiting_plan(repl, tmp_path)
    assert mode_badge(repl) == "⏸ PLAN · awaiting approval"

    plan_flow.before_turn(repl, "approve")
    assert mode_badge(repl) == "⏸ PLAN · executing"

    set_execution_mode(repl, ExecutionMode.AUTO)
    assert mode_badge(repl) == "⏵⏵ AUTO · plan pending"
    assert repl._status_state.execution_label == mode_badge(repl)


def test_leaving_plan_mode_says_the_plan_is_still_unapproved(tmp_path):
    repl = _repl(tmp_path)
    _waiting_plan(repl, tmp_path)
    repl.output.truncate(0)
    repl.output.seek(0)

    set_execution_mode(repl, ExecutionMode.MANUAL)

    assert "still waiting for approval" in repl.output.getvalue()


def test_plan_summary_ends_with_waiting_for_approval(tmp_path):
    repl = _repl(tmp_path)
    _waiting_plan(repl, tmp_path)
    out = repl.output.getvalue()
    assert "Waiting for approval." in out
    assert "Modify 1" in out


# ── AUTO has a strict workspace boundary ────────────────────────────────────


def test_switching_into_auto_drops_locations_granted_earlier(tmp_path):
    repl = _repl(tmp_path, ExecutionMode.MANUAL)
    elsewhere = tmp_path.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    repl._permission_gate.boundary.grant(elsewhere)
    assert repl._permission_gate.boundary.extra_roots

    set_execution_mode(repl, ExecutionMode.AUTO)

    assert not repl._permission_gate.boundary.extra_roots


def test_command_arguments_that_leave_the_workspace_are_detected(tmp_path):
    boundary = Boundary(tmp_path)
    assert command_paths_outside("python ../escape.py", boundary)
    assert command_paths_outside("git -C ../other status", boundary)
    assert command_paths_outside("pytest --rootdir=../other", boundary)
    assert command_paths_outside("type " + str(tmp_path.parent / "x.txt"), boundary)


def test_ordinary_commands_are_not_flagged(tmp_path):
    boundary = Boundary(tmp_path)
    for command in (
        "python script.py",
        "pytest tests/test_a.py -q",
        "git log origin/main..HEAD",
        "git diff HEAD~1",
        "python -m pip install -e .[dev]",
        "curl https://example.com/a/../b",
        "dir /s",
        "ruff check src/",
        "python " + str(tmp_path / "inside.py"),
    ):
        assert not command_paths_outside(command, boundary), command


@pytest.mark.skipif(os.name == "nt", reason="a lone /x is a switch on Windows, a path elsewhere")
def test_absolute_posix_paths_are_flagged_off_windows(tmp_path):
    assert command_paths_outside("cat /etc/passwd", Boundary(tmp_path))


def test_execute_command_describes_an_escaping_command_as_outside(tmp_path):
    from velune.tools.terminal.execute import ExecuteCommand

    tool = ExecuteCommand(workspace_path=str(tmp_path))
    boundary = Boundary(tmp_path)

    actions = tool.describe_actions({"command": "python ../escape.py"}, boundary)
    assert actions[0].outside_workspace
    assert authorize(actions, PolicyState(mode=ExecutionMode.AUTO)).verdict is Verdict.DENY

    actions = tool.describe_actions({"command": "python tool.py"}, boundary)
    assert not actions[0].outside_workspace
    assert authorize(actions, PolicyState(mode=ExecutionMode.AUTO)).verdict is Verdict.ALLOW


# ── consistent badges and the Shift+Tab confirmation ────────────────────────


def test_all_three_badges_have_a_glyph_and_the_mode_name():
    for mode, badge in BADGES.items():
        glyph, _, name = badge.partition(" ")
        assert glyph and not glyph.isalnum()
        assert name == mode.label


def test_shift_tab_flashes_a_brief_confirmation_in_the_status_bar(tmp_path):
    repl = _repl(tmp_path, ExecutionMode.MANUAL)
    cycle_execution_mode(repl)

    text = "".join(f for _, f in render_status_bar(repl._status_state))
    assert "PLAN: plans first" in text
    assert repl.output.getvalue() == ""  # still nothing in the transcript

    repl._status_state.mode_notice_until = 0.0  # time has passed
    text = "".join(f for _, f in render_status_bar(repl._status_state))
    assert "plans first" not in text
