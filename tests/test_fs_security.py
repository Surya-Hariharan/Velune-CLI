"""Filesystem tool security through the real permission gate.

Traversal, absolute paths, symlink escapes, protected files (.git, Velune hook
configuration), PLAN/AUTO contracts and denial — all asserting that the disk is
unchanged when a call is refused.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._fs_registry import gated_ctx, production_registry
from velune.permissions import ExecutionMode
from velune.permissions.actions import Risk
from velune.permissions.boundary import Boundary, path_action, protected_reason
from velune.permissions.gate import ActionDeniedError, Approval
from velune.tools.base.tool import ToolPermission, authorize_and_execute


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_bytes(b"inside\n")
    return root


async def _run(registry, name, ctx, **args):
    ctx.permissions = {ToolPermission.FILESYSTEM_WRITE, ToolPermission.FILESYSTEM_READ}
    return await authorize_and_execute(registry.get(name), ctx, **args)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ── Traversal, absolute paths, symlinks ────────────────────────────────────────

ESCAPES = ["../outside.txt", "sub/../../outside.txt", "..\\outside.txt"]


@pytest.mark.parametrize("raw", ESCAPES)
@pytest.mark.parametrize("tool", ["write_file", "edit_file", "create_file"])
async def test_auto_mode_refuses_writes_that_escape_the_workspace(ws, raw, tool):
    if "\\" in raw and os.name != "nt":
        pytest.skip("backslash is a path separator only on Windows")
    registry = production_registry(ws)
    args = {"file_path": raw}
    if tool == "write_file":
        args["content"] = "pwned"
    elif tool == "edit_file":
        args.update(old_string="inside", new_string="pwned")
    with pytest.raises(ActionDeniedError, match="outside the workspace"):
        await _run(registry, tool, gated_ctx(ws), **args)
    assert not (ws.parent / "outside.txt").exists()


async def test_absolute_path_outside_is_refused_and_inside_is_allowed(ws, tmp_path):
    registry = production_registry(ws)
    outside = tmp_path / "elsewhere.txt"
    with pytest.raises(ActionDeniedError):
        await _run(registry, "write_file", gated_ctx(ws), file_path=str(outside), content="x")
    assert not outside.exists()
    await _run(registry, "write_file", gated_ctx(ws), file_path=str(ws / "abs.txt"), content="ok")
    assert (ws / "abs.txt").read_bytes() == b"ok"


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter semantics")
async def test_windows_forward_slashes_and_case_resolve_inside(ws):
    registry = production_registry(ws)
    mixed = str(ws).upper().replace("\\", "/") + "/Sub/New.txt"
    await _run(registry, "write_file", gated_ctx(ws), file_path=mixed, content="x")
    assert (ws / "Sub" / "New.txt").read_bytes() == b"x"


def _symlink_or_skip(link: Path, target: Path, is_dir: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_dir)
        return
    except (OSError, NotImplementedError):
        pass
    if is_dir and os.name == "nt":
        # Without Developer Mode Windows refuses symlinks, but a directory junction
        # is the same escape vector and needs no privilege.
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("this platform/user cannot create symlinks")


async def test_symlinked_folder_pointing_outside_cannot_be_written_through(ws, tmp_path):
    secret_dir = tmp_path / "victim"
    secret_dir.mkdir()
    _symlink_or_skip(ws / "link", secret_dir, is_dir=True)
    registry = production_registry(ws)
    for tool, args in [
        ("write_file", {"file_path": "link/evil.txt", "content": "x"}),
        ("create_file", {"file_path": "link/evil.txt"}),
        ("create_directory", {"path": "link/evil"}),
    ]:
        with pytest.raises(ActionDeniedError):
            await _run(registry, tool, gated_ctx(ws), **args)
    assert list(secret_dir.iterdir()) == []


async def test_symlinked_file_pointing_outside_cannot_be_edited(ws, tmp_path):
    target = tmp_path / "outside.cfg"
    target.write_bytes(b"value = 1\n")
    _symlink_or_skip(ws / "cfg", target, is_dir=False)
    registry = production_registry(ws)
    with pytest.raises(ActionDeniedError):
        await _run(
            registry, "edit_file", gated_ctx(ws), file_path="cfg", old_string="1", new_string="2"
        )
    assert target.read_bytes() == b"value = 1\n"


# ── Protected paths ────────────────────────────────────────────────────────────

PROTECTED = [".git/hooks/pre-commit", ".git/config", ".velune/hooks.json", "velune.toml"]


@pytest.mark.parametrize("rel", PROTECTED)
def test_protected_paths_are_high_risk_for_changes_only(ws, rel):
    from velune.permissions.actions import ActionType

    boundary = Boundary(ws)
    assert protected_reason(ws / rel)
    change = path_action(ActionType.MODIFY_FILE, rel, boundary, "write")
    assert change.risk is Risk.HIGH
    assert "run" in change.reason  # explains why: these can run code
    read = path_action(ActionType.READ, rel, boundary, "read")
    assert read.risk is Risk.LOW


@pytest.mark.parametrize("rel", ["src/git_utils.py", ".github/workflows/ci.yml", ".gitignore"])
def test_lookalikes_are_not_protected(ws, rel):
    assert protected_reason(ws / rel) == ""


@pytest.mark.parametrize("rel", PROTECTED)
async def test_auto_mode_asks_before_touching_protected_files(ws, rel):
    asked: list[str] = []

    async def deny(tool_name, decision):
        asked.append(decision.reason)
        return Approval.DENY

    registry = production_registry(ws)
    with pytest.raises(ActionDeniedError, match="rejected"):
        await _run(registry, "write_file", gated_ctx(ws, ask=deny), file_path=rel, content="x")
    assert asked and "high-risk" in asked[0]
    assert not (ws / rel).exists()


async def test_protected_files_never_ride_a_session_grant(ws):
    calls = 0

    async def allow_for_session(tool_name, decision):
        nonlocal calls
        calls += 1
        return Approval.ALLOW_TASK

    registry = production_registry(ws)
    ctx = gated_ctx(ws, ExecutionMode.MANUAL, ask=allow_for_session)
    await _run(registry, "write_file", ctx, file_path=".git/hooks/pre-push", content="#!/bin/sh\n")
    await _run(registry, "write_file", ctx, file_path=".git/hooks/pre-push", content="#!/bin/sh\n")
    assert calls == 2  # asked both times: "always allow" does not cover protected paths


async def test_reading_git_config_needs_no_approval(ws):
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_bytes(b"[core]\n")
    registry = production_registry(ws)
    out = await _run(
        registry, "read_file", gated_ctx(ws, ExecutionMode.MANUAL), file_path=".git/config"
    )
    assert out == "[core]\n"


# ── Mode contracts and denial ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("write_file", {"file_path": "a.txt", "content": "x"}),
        ("edit_file", {"file_path": "a.txt", "old_string": "inside", "new_string": "x"}),
        ("create_file", {"file_path": "n.txt"}),
        ("delete_file", {"file_path": "a.txt"}),
        ("create_directory", {"path": "d"}),
        ("move_path", {"source": "a.txt", "destination": "b.txt"}),
    ],
)
async def test_plan_mode_changes_nothing(ws, tool, args):
    before = _snapshot(ws)
    with pytest.raises(ActionDeniedError, match="plan mode"):
        await _run(production_registry(ws), tool, gated_ctx(ws, ExecutionMode.PLAN), **args)
    assert _snapshot(ws) == before
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt"]


async def test_manual_denial_leaves_the_disk_untouched(ws):
    async def deny(tool_name, decision):
        return Approval.DENY

    with pytest.raises(ActionDeniedError):
        await _run(
            production_registry(ws),
            "edit_file",
            gated_ctx(ws, ExecutionMode.MANUAL, ask=deny),
            file_path="a.txt",
            old_string="inside",
            new_string="changed",
        )
    assert (ws / "a.txt").read_bytes() == b"inside\n"


async def test_no_gate_fails_closed_for_edit_file(ws):
    from velune.tools.base.tool import ToolCallContext

    ctx = ToolCallContext(run_id="t", actor="t", workspace=ws)
    with pytest.raises(ActionDeniedError, match="fail closed"):
        await _run(
            production_registry(ws),
            "edit_file",
            ctx,
            file_path="a.txt",
            old_string="inside",
            new_string="x",
        )
    assert (ws / "a.txt").read_bytes() == b"inside\n"


async def test_recursive_delete_stays_high_risk_in_auto(ws):
    (ws / "pkg").mkdir()
    (ws / "pkg" / "m.py").write_bytes(b"x")
    asked: list[bool] = []

    async def deny(tool_name, decision):
        asked.append(decision.high_risk)
        return Approval.DENY

    with pytest.raises(ActionDeniedError):
        await _run(
            production_registry(ws),
            "delete_directory",
            gated_ctx(ws, ask=deny),
            path="pkg",
            recursive=True,
        )
    assert asked == [True]
    assert (ws / "pkg" / "m.py").exists()


def test_atomic_write_rechecks_the_boundary_right_before_the_swap(ws, tmp_path):
    from velune.execution.path_guard import PathTraversalError
    from velune.permissions.gate import reset_active_boundary, set_active_boundary
    from velune.tools.filesystem.mutation import atomic_write

    token = set_active_boundary(Boundary(ws))
    try:
        with pytest.raises(PathTraversalError):
            atomic_write(tmp_path / "escaped.txt", b"x")
    finally:
        reset_active_boundary(token)
    assert not (tmp_path / "escaped.txt").exists()
