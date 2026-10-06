"""Folder create/delete and move/rename tools, through the permission gate.

Before these existed the only way to make a folder was `execute_command
mkdir …`, which the sandbox rejects (mkdir is not on the allowlist, and on
Windows it is a cmd.exe builtin that a shell-less sandbox can't run at all) —
the failure in the original bug report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.permissions import ExecutionMode, PolicyState
from velune.permissions.actions import Risk
from velune.permissions.boundary import Boundary
from velune.permissions.gate import ActionDeniedError, Approval, PermissionGate
from velune.tools.base.tool import ToolCallContext, ToolPermission, authorize_and_execute
from velune.tools.filesystem.dirs import CreateDirectory, DeleteDirectory, MovePath


def _ctx(ws: Path, mode=ExecutionMode.AUTO, ask=None) -> ToolCallContext:
    return ToolCallContext(
        run_id="t",
        actor="test",
        workspace=ws,
        permissions={ToolPermission.FILESYSTEM_WRITE},
        gate=PermissionGate(PolicyState(mode=mode), Boundary(ws), ask=ask),
    )


async def _yes(tool_name, decision):
    return Approval.ALLOW_ONCE


async def test_create_folder_with_spaces_like_the_bug_report(tmp_path):
    out = await authorize_and_execute(CreateDirectory(tmp_path), _ctx(tmp_path), path="Project 1")
    assert (tmp_path / "Project 1").is_dir()
    assert "Created folder" in out


async def test_create_nested_folders_and_is_idempotent(tmp_path):
    tool = CreateDirectory(tmp_path)
    await authorize_and_execute(tool, _ctx(tmp_path), path="a/b/c")
    again = await authorize_and_execute(tool, _ctx(tmp_path), path="a/b/c")
    assert (tmp_path / "a" / "b" / "c").is_dir()
    assert "already existed" in again


async def test_manual_mode_asks_before_creating_a_folder(tmp_path):
    with pytest.raises(ActionDeniedError):  # nobody to ask → denied
        await authorize_and_execute(
            CreateDirectory(tmp_path), _ctx(tmp_path, ExecutionMode.MANUAL), path="x"
        )
    assert not (tmp_path / "x").exists()
    await authorize_and_execute(
        CreateDirectory(tmp_path), _ctx(tmp_path, ExecutionMode.MANUAL, _yes), path="x"
    )
    assert (tmp_path / "x").is_dir()


async def test_plan_mode_never_creates_folders(tmp_path):
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            CreateDirectory(tmp_path), _ctx(tmp_path, ExecutionMode.PLAN, _yes), path="x"
        )
    assert not (tmp_path / "x").exists()


async def test_folder_outside_workspace_is_refused_in_auto_and_asked_in_manual(tmp_path):
    ws, desktop = tmp_path / "ws", tmp_path / "Desktop"
    ws.mkdir()
    desktop.mkdir()
    target = str(desktop / "Project 1")
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(CreateDirectory(ws), _ctx(ws), path=target)
    assert not Path(target).exists()

    seen = []

    async def allow_once(tool_name, decision):
        seen.append(decision)
        return Approval.ALLOW_ONCE

    # AUTO refuses even when someone could answer; MANUAL asks.
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(CreateDirectory(ws), _ctx(ws, ask=allow_once), path=target)
    assert not seen

    manual = _ctx(ws, mode=ExecutionMode.MANUAL, ask=allow_once)
    await authorize_and_execute(CreateDirectory(ws), manual, path=target)
    assert Path(target).is_dir()
    assert seen and seen[0].outside_workspace


async def test_delete_empty_folder_is_ordinary_but_recursive_is_high_risk(tmp_path):
    (tmp_path / "empty").mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "f.txt").write_text("x")
    tool, b = DeleteDirectory(tmp_path), Boundary(tmp_path)
    assert tool.describe_actions({"path": "empty"}, b)[0].risk is Risk.MEDIUM
    assert tool.describe_actions({"path": "full", "recursive": True}, b)[0].risk is Risk.HIGH
    assert tool.describe_actions({"path": "."}, b)[0].risk is Risk.HIGH  # the workspace itself

    await authorize_and_execute(tool, _ctx(tmp_path), path="empty")
    assert not (tmp_path / "empty").exists()
    # Recursive delete in AUTO still needs explicit confirmation.
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(tool, _ctx(tmp_path), path="full", recursive=True)
    assert full.exists()
    await authorize_and_execute(tool, _ctx(tmp_path, ask=_yes), path="full", recursive=True)
    assert not full.exists()


async def test_non_empty_folder_requires_recursive(tmp_path):
    full = tmp_path / "full"
    full.mkdir()
    (full / "f.txt").write_text("x")
    with pytest.raises(OSError, match="not empty"):
        await authorize_and_execute(DeleteDirectory(tmp_path), _ctx(tmp_path), path="full")


async def test_move_and_rename(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    tool = MovePath(tmp_path)
    await authorize_and_execute(tool, _ctx(tmp_path), source="a.txt", destination="docs/b.txt")
    assert (tmp_path / "docs" / "b.txt").read_text() == "hi"
    assert not (tmp_path / "a.txt").exists()
    (tmp_path / "old_dir").mkdir()
    await authorize_and_execute(tool, _ctx(tmp_path), source="old_dir", destination="new_dir")
    assert (tmp_path / "new_dir").is_dir()


async def test_move_refuses_to_clobber_without_overwrite(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    with pytest.raises(FileExistsError):
        await authorize_and_execute(
            MovePath(tmp_path), _ctx(tmp_path), source="a.txt", destination="b.txt"
        )
    assert (tmp_path / "b.txt").read_text() == "b"


async def test_moving_into_a_location_outside_the_workspace_is_caught(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    with pytest.raises(ActionDeniedError):
        await authorize_and_execute(
            MovePath(ws), _ctx(ws), source="a.txt", destination=str(tmp_path / "out.txt")
        )
    assert (ws / "a.txt").exists()


def test_new_tools_are_in_the_real_registry(tmp_path):
    from types import SimpleNamespace

    from velune.tools.subsystems import _create_tool_registry

    services = {"runtime.execution_executor": SimpleNamespace(sandbox=None)}
    container = SimpleNamespace(get=lambda k: services[k], has=lambda k: k in services)
    reg = _create_tool_registry(SimpleNamespace(workspace=tmp_path, container=container))
    assert {"create_directory", "delete_directory", "move_path"} <= set(reg.list_tools())


async def test_allow_once_does_not_persist_but_allow_for_task_does(tmp_path):
    ws, desktop = tmp_path / "ws", tmp_path / "Desktop"
    ws.mkdir()
    desktop.mkdir()
    outside_flags: list[bool] = []

    def answer_with(choice):
        async def ask(tool_name, decision):
            outside_flags.append(decision.outside_workspace)
            return choice

        return ask

    ctx = _ctx(ws, mode=ExecutionMode.MANUAL, ask=answer_with(Approval.ALLOW_ONCE))
    await authorize_and_execute(CreateDirectory(ws), ctx, path=str(desktop / "one"))
    await authorize_and_execute(CreateDirectory(ws), ctx, path=str(desktop / "one" / "two"))
    assert outside_flags == [True, True]  # "once" really is once: still outside the next time

    outside_flags.clear()
    ctx = _ctx(ws, mode=ExecutionMode.MANUAL, ask=answer_with(Approval.ALLOW_TASK))
    await authorize_and_execute(CreateDirectory(ws), ctx, path=str(desktop / "proj"))
    await authorize_and_execute(CreateDirectory(ws), ctx, path=str(desktop / "proj" / "sub"))
    assert outside_flags == [True, False]  # granted for the task: no longer "outside"
    assert (desktop / "proj" / "sub").is_dir()
