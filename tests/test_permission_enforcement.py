"""Permissions are enforced in code at the tool layer and can't be bypassed.

Covers the real tool registry (so a tool added later is covered too), the
fail-closed default, the boundary for paths and command directories, the
user-command tools (/push), council edits, and the Docker sandbox.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.permissions import ExecutionMode, PolicyState
from velune.permissions.boundary import Boundary
from velune.permissions.gate import ActionDeniedError, Approval, PermissionGate
from velune.tools.base.tool import BaseTool, ToolCallContext, ToolPermission, authorize_and_execute

MUTATING_PERMS = {
    ToolPermission.FILESYSTEM_WRITE,
    ToolPermission.GIT_WRITE,
    ToolPermission.TERMINAL_EXECUTE,
}


def _registry(ws: Path):
    """The registry the REPL really builds (velune/tools/subsystems.py)."""
    from velune.tools.subsystems import _create_tool_registry

    services = {
        "runtime.execution_executor": SimpleNamespace(sandbox=None),
    }
    container = SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services)
    return _create_tool_registry(SimpleNamespace(workspace=ws, container=container))


def _ctx(ws: Path, mode: ExecutionMode | None, ask=None, *, perms=None) -> ToolCallContext:
    gate = None
    if mode is not None:
        gate = PermissionGate(PolicyState(mode=mode), Boundary(ws), ask=ask)
    return ToolCallContext(
        run_id="t",
        actor="test",
        workspace=ws,
        permissions=set(perms if perms is not None else ToolPermission),
        gate=gate,
    )


def _sample_args(tool: BaseTool) -> dict:
    props = (tool.get_schema() or {}).get("properties", {})
    defaults = {
        "file_path": "probe.txt",
        "content": "x",
        "path": "probe-dir",
        "source": "probe.txt",
        "destination": "moved.txt",
        "command": "pytest -q",
        "message": "msg",
        "branch": "main",
        "url": "https://example.com",
    }
    return {k: defaults.get(k, "x") for k in props if k in defaults}


def _mutating_tools(ws: Path) -> list[BaseTool]:
    reg = _registry(ws)
    tools = [reg.get(name) for name in reg.list_tools()]
    return [t for t in tools if t is not None and t.get_required_permissions() & MUTATING_PERMS]


def test_registry_has_mutating_tools_to_check(tmp_path):
    names = {t.get_name() for t in _mutating_tools(tmp_path)}
    assert {"write_file", "delete_file", "execute_command", "git_commit"} <= names


@pytest.mark.parametrize("mode", [None, ExecutionMode.PLAN, ExecutionMode.MANUAL])
async def test_every_mutating_tool_is_refused_without_approval(tmp_path, mode):
    """No gate (fail closed), PLAN (deny), MANUAL with nobody to ask (deny)."""
    for tool in _mutating_tools(tmp_path):
        with pytest.raises(PermissionError):
            await authorize_and_execute(tool, _ctx(tmp_path, mode), **_sample_args(tool))
    assert not (tmp_path / "probe.txt").exists()


async def test_a_tool_that_forgets_describe_actions_is_still_gated(tmp_path):
    class Sneaky(BaseTool):
        ran = False

        def get_name(self):
            return "sneaky"

        def get_description(self):
            return ""

        def get_required_permissions(self):
            return {ToolPermission.FILESYSTEM_WRITE}

        async def execute(self, **kw):
            Sneaky.ran = True

    for mode in (None, ExecutionMode.PLAN):
        with pytest.raises(PermissionError):
            await authorize_and_execute(Sneaky(), _ctx(tmp_path, mode))
    assert Sneaky.ran is False


async def test_auto_mode_lets_the_agent_write(tmp_path):
    from velune.tools.filesystem.write import WriteFile

    tool = WriteFile(workspace=tmp_path, confirm=False)
    await authorize_and_execute(
        tool, _ctx(tmp_path, ExecutionMode.AUTO), file_path="a.txt", content="hi"
    )
    assert (tmp_path / "a.txt").read_text() == "hi"


async def test_manual_mode_writes_after_the_user_approves(tmp_path):
    from velune.tools.filesystem.write import WriteFile

    async def approve(tool_name, decision):
        return Approval.ALLOW_ONCE

    tool = WriteFile(workspace=tmp_path, confirm=False)
    await authorize_and_execute(
        tool, _ctx(tmp_path, ExecutionMode.MANUAL, approve), file_path="a.txt", content="hi"
    )
    assert (tmp_path / "a.txt").exists()


async def test_outside_workspace_needs_a_grant_and_then_works(tmp_path):
    from velune.tools.filesystem.write import WriteFile

    ws, outside = tmp_path / "ws", tmp_path / "Desktop"
    ws.mkdir()
    outside.mkdir()
    tool = WriteFile(workspace=ws, confirm=False)
    target = str(outside / "note.txt")

    async def deny(tool_name, decision):
        assert decision.outside_workspace
        return Approval.DENY

    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            tool, _ctx(ws, ExecutionMode.MANUAL, deny), file_path=target, content="x"
        )
    assert not Path(target).exists()

    async def allow_task(tool_name, decision):
        return Approval.ALLOW_TASK

    ctx = _ctx(ws, ExecutionMode.MANUAL, allow_task)
    await authorize_and_execute(tool, ctx, file_path=target, content="x")
    assert Path(target).read_text() == "x"
    assert ctx.gate.boundary.inside(Path(target).resolve())  # granted for the task


async def test_auto_never_writes_outside_the_workspace_even_if_asked_nicely(tmp_path):
    from velune.tools.filesystem.write import WriteFile

    ws, outside = tmp_path / "ws", tmp_path / "Desktop"
    ws.mkdir()
    outside.mkdir()
    target = str(outside / "note.txt")

    async def would_allow(tool_name, decision):
        raise AssertionError("AUTO must refuse without asking")

    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            WriteFile(workspace=ws, confirm=False),
            _ctx(ws, ExecutionMode.AUTO, would_allow),
            file_path=target,
            content="x",
        )
    assert not Path(target).exists()


async def test_command_directory_outside_workspace_is_not_trusted(tmp_path):
    """A model-supplied `directory` used to become the sandbox workspace."""
    from velune.tools.terminal.execute import ExecuteCommand

    ws = tmp_path / "ws"
    ws.mkdir()
    tool = ExecuteCommand(workspace_path=str(ws))
    actions = tool.describe_actions({"command": "pytest", "directory": str(tmp_path)}, Boundary(ws))
    assert actions[0].outside_workspace
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            tool, _ctx(ws, ExecutionMode.AUTO), command="pytest", directory=str(tmp_path)
        )


def test_read_only_commands_are_reads_and_high_risk_is_flagged(tmp_path):
    from velune.permissions.actions import ActionType, Risk
    from velune.tools.terminal.execute import ExecuteCommand

    tool, b = ExecuteCommand(), Boundary(tmp_path)
    assert tool.describe_actions({"command": "git status"}, b)[0].action_type is ActionType.READ
    reset = tool.describe_actions({"command": "git reset --hard"}, b)[0]
    assert reset.risk is Risk.HIGH
    assert tool.describe_actions({"command": "sudo ls"}, b)[0].blocked


def test_secret_files_are_flagged_even_for_reads(tmp_path):
    from velune.tools.filesystem.read import ReadFile

    action = ReadFile(workspace=tmp_path).describe_actions(
        {"file_path": ".env"}, Boundary(tmp_path)
    )[0]
    assert action.secret


def test_force_push_is_high_risk(tmp_path):
    from velune.permissions.actions import Risk
    from velune.tools.git.providers import GitPushTool

    tool = GitPushTool(workspace=tmp_path)
    assert tool.describe_actions({"force": True}, Boundary(tmp_path))[0].risk is Risk.HIGH
    assert tool.describe_actions({}, Boundary(tmp_path))[0].risk is not Risk.HIGH


async def test_council_edits_are_refused_in_plan_mode(tmp_path):
    from velune.cli.handlers.council import _authorize_council_edits

    repl = _repl(tmp_path, ExecutionMode.PLAN)
    assert await _authorize_council_edits(repl, {tmp_path / "a.py": "x"}) is None
    repl._execution_mode = ExecutionMode.AUTO
    assert await _authorize_council_edits(repl, {tmp_path / "a.py": "x"}) is True
    repl._execution_mode = ExecutionMode.MANUAL
    assert await _authorize_council_edits(repl, {tmp_path / "a.py": "x"}) is False


def _repl(ws: Path, mode: ExecutionMode):
    import io

    from rich.console import Console

    services = {"runtime.workspace": str(ws)}
    return SimpleNamespace(
        console=Console(file=io.StringIO()),
        container=SimpleNamespace(get=lambda k: services[k], has=lambda k: k in services),
        _execution_mode=mode,
        _tool_session_grants=set(),
        _mcp_registry=None,
        _session_id="t",
    )


def test_docker_sandbox_validates_commands(tmp_path, monkeypatch):
    from velune.core.errors.execution import SandboxError
    from velune.execution.command_spec import CommandSpec
    from velune.execution.docker_sandbox import DockerSandbox

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox._started = True
    sandbox.workspace_path = tmp_path
    monkeypatch.setattr(sandbox, "_require_started", lambda: None, raising=False)
    spec = CommandSpec(executable="curl", args=("evil",), cwd=tmp_path)
    with pytest.raises(SandboxError):
        sandbox.execute(spec)
