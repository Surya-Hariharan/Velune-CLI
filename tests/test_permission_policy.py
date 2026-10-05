"""The execution-mode policy matrix: what each mode allows, denies or asks about."""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.permissions import Action, ActionType, ExecutionMode, PolicyState, Risk, Verdict
from velune.permissions import authorize as decide
from velune.permissions.boundary import Boundary, is_secret_path
from velune.permissions.commands import CommandClass, classify

WS = Path("/ws").resolve()


def _act(kind: ActionType, target: str = "src/a.py", **kw) -> Action:
    return Action(kind, str(WS / target), **kw)


def _state(mode: ExecutionMode, **kw) -> PolicyState:
    return PolicyState(mode=mode, plans_dir=WS / ".velune" / "plans", **kw)


WRITES = [
    ActionType.CREATE_FILE,
    ActionType.MODIFY_FILE,
    ActionType.DELETE_FILE,
    ActionType.CREATE_DIRECTORY,
    ActionType.DELETE_DIRECTORY,
    ActionType.MOVE,
    ActionType.RUN_COMMAND,
    ActionType.GIT_WRITE,
]


# ── MANUAL ──────────────────────────────────────────────────────────────────


def test_manual_reads_freely():
    assert decide([_act(ActionType.READ)], _state(ExecutionMode.MANUAL)).verdict is Verdict.ALLOW


@pytest.mark.parametrize("kind", WRITES)
def test_manual_asks_before_every_change(kind):
    assert decide([_act(kind)], _state(ExecutionMode.MANUAL)).verdict is Verdict.ASK


# ── PLAN ────────────────────────────────────────────────────────────────────


def test_plan_reads_freely():
    assert decide([_act(ActionType.READ)], _state(ExecutionMode.PLAN)).verdict is Verdict.ALLOW


@pytest.mark.parametrize("kind", WRITES)
def test_plan_blocks_every_change_while_planning(kind):
    decision = decide([_act(kind)], _state(ExecutionMode.PLAN))
    assert decision.verdict is Verdict.DENY
    assert "write_plan" in decision.reason


def test_plan_may_write_its_own_plan_file():
    plan = _act(ActionType.WRITE_PLAN, ".velune/plans/fix-auth.md")
    assert decide([plan], _state(ExecutionMode.PLAN)).verdict is Verdict.ALLOW


def test_plan_file_tool_cannot_write_elsewhere():
    sneaky = _act(ActionType.WRITE_PLAN, "src/app.py")
    assert decide([sneaky], _state(ExecutionMode.PLAN)).verdict is Verdict.DENY


def test_executing_an_approved_plan_allows_its_files_and_asks_on_scope_change():
    state = _state(
        ExecutionMode.PLAN, plan_executing=True, plan_scope=frozenset({WS / "src" / "a.py"})
    )
    assert decide([_act(ActionType.MODIFY_FILE, "src/a.py")], state).verdict is Verdict.ALLOW
    scope_change = decide([_act(ActionType.MODIFY_FILE, "src/other.py")], state)
    assert scope_change.verdict is Verdict.ASK
    assert "scope change" in scope_change.reason
    assert decide([_act(ActionType.RUN_COMMAND, "pytest")], state).verdict is Verdict.ALLOW


# ── AUTO ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", WRITES)
def test_auto_allows_ordinary_changes_inside_the_workspace(kind):
    assert decide([_act(kind)], _state(ExecutionMode.AUTO)).verdict is Verdict.ALLOW


def test_auto_still_confirms_high_risk():
    risky = _act(ActionType.RUN_COMMAND, "git reset --hard", risk=Risk.HIGH)
    decision = decide([risky], _state(ExecutionMode.AUTO))
    assert decision.verdict is Verdict.ASK
    assert decision.high_risk


def test_auto_still_confirms_outside_the_workspace():
    outside = Action(ActionType.CREATE_DIRECTORY, "C:/Users/me/Desktop/x", outside_workspace=True)
    decision = decide([outside], _state(ExecutionMode.AUTO))
    assert decision.verdict is Verdict.ASK
    assert decision.outside_workspace


def test_auto_still_confirms_touching_secrets():
    env = _act(ActionType.MODIFY_FILE, ".env", secret=True)
    assert decide([env], _state(ExecutionMode.AUTO)).verdict is Verdict.ASK
    read_env = _act(ActionType.READ, ".env", secret=True)
    assert decide([read_env], _state(ExecutionMode.AUTO)).verdict is Verdict.ASK


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_blocked_commands_are_denied_in_every_mode(mode):
    sudo = _act(ActionType.RUN_COMMAND, "sudo rm -rf /", blocked=True)
    assert decide([sudo], _state(mode)).verdict is Verdict.DENY


def test_network_asks_except_in_auto():
    fetch = Action(ActionType.NETWORK, "https://example.com")
    assert decide([fetch], _state(ExecutionMode.MANUAL)).verdict is Verdict.ASK
    assert decide([fetch], _state(ExecutionMode.AUTO)).verdict is Verdict.ALLOW


def test_batch_reports_only_the_actions_needing_approval():
    batch = [_act(ActionType.READ), _act(ActionType.MODIFY_FILE), _act(ActionType.CREATE_FILE)]
    decision = decide(batch, _state(ExecutionMode.MANUAL))
    assert decision.verdict is Verdict.ASK
    assert [a.action_type for a in decision.actions] == [
        ActionType.MODIFY_FILE,
        ActionType.CREATE_FILE,
    ]


def test_mode_cycle_order():
    assert ExecutionMode.MANUAL.next() is ExecutionMode.PLAN
    assert ExecutionMode.PLAN.next() is ExecutionMode.AUTO
    assert ExecutionMode.AUTO.next() is ExecutionMode.MANUAL


# ── Commands ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", CommandClass.READ_ONLY),
        ("git diff --stat", CommandClass.READ_ONLY),
        ("pytest -q", CommandClass.MUTATING),
        ("npm install", CommandClass.MUTATING),
        ("ruff check --fix .", CommandClass.MUTATING),
        ("git reset --hard HEAD", CommandClass.HIGH_RISK),
        ("git clean -fd", CommandClass.HIGH_RISK),
        ("git push --force origin main", CommandClass.HIGH_RISK),
        ("git push -f", CommandClass.HIGH_RISK),
        ("Remove-Item build -Recurse -Force", CommandClass.HIGH_RISK),
        ("npm install -g typescript", CommandClass.HIGH_RISK),
        ("winget install git", CommandClass.HIGH_RISK),
        ("setx PATH foo", CommandClass.HIGH_RISK),
        ("sudo apt update", CommandClass.BLOCKED),
    ],
)
def test_command_classification(command, expected):
    assert classify(command)[0] is expected


# ── Boundary ────────────────────────────────────────────────────────────────


def test_boundary_anchors_relative_paths_to_the_workspace(tmp_path):
    boundary = Boundary(tmp_path)
    assert boundary.resolve("src/a.py") == (tmp_path / "src" / "a.py").resolve()
    assert boundary.inside(boundary.resolve("src/a.py"))
    assert not boundary.inside(boundary.resolve("../elsewhere"))


def test_boundary_task_grants(tmp_path):
    ws = tmp_path / "ws"
    other = tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    boundary = Boundary(ws)
    target = (other / "x.txt").resolve()
    assert not boundary.inside(target)
    boundary.grant(other)
    assert boundary.inside(target)
    boundary.clear_grants()
    assert not boundary.inside(target)


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        (".env.production", True),
        (".env.example", False),
        ("certs/server.pem", True),
        ("home/.ssh/config", True),
        ("src/app.py", False),
    ],
)
def test_secret_paths(path, secret):
    assert is_secret_path(Path(path)) is secret


def test_plan_files_are_only_free_while_planning():
    """Seen live: after a MANUAL rejection the model wrote a plan file unasked."""
    plan = _act(ActionType.WRITE_PLAN, ".velune/plans/x.md")
    assert decide([plan], _state(ExecutionMode.PLAN)).verdict is Verdict.ALLOW
    assert decide([plan], _state(ExecutionMode.MANUAL)).verdict is Verdict.ASK
    assert decide([plan], _state(ExecutionMode.AUTO)).verdict is Verdict.ALLOW
