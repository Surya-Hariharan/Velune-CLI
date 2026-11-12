"""Protected-path enforcement beyond the obvious spellings (CodeRabbit #4 and #5 on PR #26).

Every test here goes through the production tool registry and the real permission gate in AUTO
mode — the mode in which only high-risk actions are put to the user. A refused call must leave
the disk untouched, and nothing here runs a hook.

* Folders: ``.velune`` itself is protected, and moving a folder carries its contents with it, so
  a move is judged by what it would install at the destination.
* Links: a path is judged by the name it was asked for *and* by where it really leads, and a
  protected location that is itself a link protects the place it points at.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests._fs_registry import gated_ctx, production_registry
from velune.hooks.config import load_hooks
from velune.permissions import ExecutionMode
from velune.permissions import boundary as boundary_mod
from velune.permissions.actions import ActionType, Risk
from velune.permissions.boundary import Boundary, path_action
from velune.permissions.gate import ActionDeniedError, Approval
from velune.tools.base.tool import ToolPermission, authorize_and_execute

HOOKS = json.dumps(
    {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo attacker"}]}]}}
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_bytes(b"inside\n")
    return root


class Asked:
    """An ``ask`` callback that records every question and refuses it."""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def __call__(self, tool_name, decision):
        self.reasons.append(decision.reason)
        return Approval.DENY

    def __len__(self) -> int:
        return len(self.reasons)


async def _run(registry, name, ctx, **args):
    ctx.permissions = {ToolPermission.FILESYSTEM_WRITE, ToolPermission.FILESYSTEM_READ}
    return await authorize_and_execute(registry.get(name), ctx, **args)


def _link_or_skip(link: Path, target: Path, is_dir: bool) -> None:
    """Symlink, or a directory junction on Windows without symlink privilege; else skip."""
    try:
        link.symlink_to(target, target_is_directory=is_dir)
        return
    except (OSError, NotImplementedError):
        pass
    if is_dir and os.name == "nt":
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("this platform/user cannot create this kind of link")


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under *root* with its bytes. Links are listed, never entered."""
    found: dict[str, bytes | None] = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        for name in dirs:
            found[(base / name).relative_to(root).as_posix()] = None
        for name in files:
            path = base / name
            found[path.relative_to(root).as_posix()] = path.read_bytes() if path.is_file() else None
    return found


async def _must_ask(ws: Path, tool: str, **args) -> Asked:
    """Run *tool* in AUTO mode, expect a refused question, and prove the disk did not change."""
    before = _tree(ws)
    asked = Asked()
    with pytest.raises(ActionDeniedError):
        await _run(
            production_registry(ws), tool, gated_ctx(ws, ExecutionMode.AUTO, ask=asked), **args
        )
    assert len(asked) >= 1, "AUTO mode should have put this to the user"
    assert _tree(ws) == before, "a refused call must leave the disk untouched"
    return asked


async def _must_not_ask(ws: Path, tool: str, **args):
    asked = Asked()
    out = await _run(
        production_registry(ws), tool, gated_ctx(ws, ExecutionMode.AUTO, ask=asked), **args
    )
    assert len(asked) == 0, f"an ordinary operation must not be blocked: {asked.reasons}"
    return out


def _live_hook_commands(ws: Path) -> list[str]:
    """What a trusted workspace would load (reading the config only — nothing is executed)."""
    return [str(b) for b in load_hooks(ws, trusted=True)]


# ── #4: the .velune folder, and moves that install protected files ────────────────────────────


async def test_moving_a_staged_folder_onto_dot_velune_asks_and_installs_nothing(ws):
    """The reported bypass: stage hooks.json elsewhere, then move the folder into place."""
    await _must_not_ask(ws, "write_file", file_path="staged/hooks.json", content=HOOKS)
    asked = await _must_ask(ws, "move_path", source="staged", destination=".velune")
    assert "high-risk" in asked.reasons[0]
    assert not (ws / ".velune").exists()
    assert _live_hook_commands(ws) == []


async def test_replacing_an_existing_dot_velune_asks_at_every_step(ws):
    (ws / ".velune").mkdir()
    (ws / ".velune" / "hooks.json").write_text('{"hooks": {}}')
    await _must_not_ask(ws, "write_file", file_path="staged/hooks.json", content=HOOKS)
    await _must_ask(ws, "move_path", source=".velune", destination="velune_old")
    # Even with the original out of the way the folder still may not be put back unasked.
    (ws / ".velune").rename(ws / "velune_old")
    await _must_ask(ws, "move_path", source="staged", destination=".velune")
    assert not (ws / ".velune").exists()


async def test_creating_and_deleting_dot_velune_ask(ws):
    await _must_ask(ws, "create_directory", path=".velune")
    (ws / ".velune").mkdir()
    await _must_ask(ws, "delete_directory", path=".velune")  # empty: used to be MEDIUM
    assert (ws / ".velune").is_dir()


async def test_dot_velune_matches_in_any_case(ws):
    await _must_ask(ws, "create_directory", path=".VELUNE")


async def test_overwriting_the_hook_file_by_a_move_asks(ws):
    (ws / ".velune").mkdir()
    (ws / ".velune" / "hooks.json").write_text('{"hooks": {}}')
    (ws / "evil.json").write_text(HOOKS)
    await _must_ask(
        ws, "move_path", source="evil.json", destination=".velune/hooks.json", overwrite=True
    )
    assert (ws / ".velune" / "hooks.json").read_text() == '{"hooks": {}}'


@pytest.mark.parametrize(
    ("protected_inside", "is_dir"),
    [
        (".velune/hooks.json", False),
        (".git/config", False),
        ("velune.toml", False),
        (".velune", True),
    ],
)
async def test_moving_a_folder_that_holds_protected_files_asks(ws, protected_inside, is_dir):
    """The destination is not itself protected; what the folder carries is."""
    inner = ws / "bundle" / protected_inside
    if is_dir:
        inner.mkdir(parents=True)
    else:
        inner.parent.mkdir(parents=True, exist_ok=True)
        inner.write_text("x")
    asked = await _must_ask(ws, "move_path", source="bundle", destination="elsewhere")
    assert any(
        "protected" in r or "hook" in r or "git" in r or "Velune" in r for r in asked.reasons
    )
    assert (ws / "bundle").is_dir()


async def test_a_protected_file_buried_deep_in_a_moved_folder_is_found(ws):
    deep = ws / "a" / "b" / "c" / "d" / ".velune"
    deep.mkdir(parents=True)
    (deep / "hooks.json").write_text(HOOKS)
    await _must_ask(ws, "move_path", source="a", destination="moved")


async def test_ordinary_folder_moves_and_renames_are_not_blocked(ws):
    """The fix must not turn every directory operation into a question."""
    (ws / "pkg").mkdir()
    (ws / "pkg" / "mod.py").write_text("x = 1\n")
    (ws / "pkg" / "data").mkdir()
    (ws / "pkg" / "data" / "gitlike.txt").write_text("not git\n")
    (ws / ".github").mkdir()
    (ws / ".github" / "ci.yml").write_text("on: push\n")
    await _must_not_ask(ws, "move_path", source="pkg", destination="renamed")
    await _must_not_ask(ws, "move_path", source=".github", destination="gh")
    await _must_not_ask(ws, "create_directory", path="new/nested")
    await _must_not_ask(ws, "delete_directory", path="new/nested")
    assert (ws / "renamed" / "mod.py").read_text() == "x = 1\n"


async def test_a_folder_too_large_to_inspect_is_put_to_the_user(ws, monkeypatch):
    monkeypatch.setattr(boundary_mod, "MAX_MOVE_SCAN_ENTRIES", 5)
    (ws / "big").mkdir()
    for i in range(12):
        (ws / "big" / f"f{i}.txt").write_text("x")
    asked = await _must_ask(ws, "move_path", source="big", destination="moved")
    assert any("too large to inspect" in r for r in asked.reasons)


async def test_a_folder_nested_too_deeply_is_put_to_the_user(ws, monkeypatch):
    monkeypatch.setattr(boundary_mod, "MAX_MOVE_SCAN_DEPTH", 3)
    deep = ws / "n"
    for _ in range(6):
        deep = deep / "d"
    deep.mkdir(parents=True)
    await _must_ask(ws, "move_path", source="n", destination="moved")


def test_the_walk_does_not_follow_links_out_of_the_tree(ws, tmp_path):
    """A link inside the moved folder is not entered, so the walk cannot be sent elsewhere."""
    outside = tmp_path / "outside"
    (outside / ".velune").mkdir(parents=True)
    (ws / "tree").mkdir()
    _link_or_skip(ws / "tree" / "link", outside, is_dir=True)
    # Whatever the link points at is not part of what the move installs.
    assert boundary_mod.protected_descendant_reason(ws / "tree", ws / "moved") == ""


@pytest.mark.parametrize(
    "rel", [".git/hooks/pre-commit", ".git/config", ".velune/hooks.json", "velune.toml"]
)
async def test_the_original_protections_are_unchanged(ws, rel):
    await _must_ask(ws, "write_file", file_path=rel, content="x")


def test_protected_reason_still_ignores_lookalikes():
    for rel in (
        "src/git_utils.py",
        ".github/workflows/ci.yml",
        ".gitignore",
        "velune.toml.bak",
        "x/hooks.json",
    ):
        assert boundary_mod.protected_reason(Path("/w") / rel) == ""


def test_the_velune_folder_is_high_risk_for_changes_only(ws):
    boundary = Boundary(ws)
    for kind in (ActionType.CREATE_DIRECTORY, ActionType.DELETE_DIRECTORY, ActionType.MOVE):
        assert path_action(kind, ".velune", boundary, "x").risk is Risk.HIGH
    assert path_action(ActionType.READ, ".velune", boundary, "x").risk is Risk.LOW


# ── #5: links ─────────────────────────────────────────────────────────────────────────────────


async def test_writing_hooks_through_a_dot_velune_link_asks(ws):
    """``.velune`` is a link to an ordinary folder; writing through the protected name must ask."""
    (ws / "cfgdir").mkdir()
    (ws / "cfgdir" / "hooks.json").write_text('{"hooks": {}}')
    _link_or_skip(ws / ".velune", ws / "cfgdir", is_dir=True)
    await _must_ask(ws, "write_file", file_path=".velune/hooks.json", content=HOOKS)
    assert (ws / "cfgdir" / "hooks.json").read_text() == '{"hooks": {}}'


async def test_writing_the_target_of_a_dot_velune_link_by_its_own_name_asks(ws):
    """The reverse direction: ``cfgdir/hooks.json`` *is* the live hook file."""
    (ws / "cfgdir").mkdir()
    (ws / "cfgdir" / "hooks.json").write_text('{"hooks": {}}')
    _link_or_skip(ws / ".velune", ws / "cfgdir", is_dir=True)
    await _must_ask(ws, "write_file", file_path="cfgdir/hooks.json", content=HOOKS)
    await _must_ask(
        ws, "edit_file", file_path="cfgdir/hooks.json", old_string="hooks", new_string="h"
    )
    assert (ws / "cfgdir" / "hooks.json").read_text() == '{"hooks": {}}'


async def test_moving_or_deleting_the_target_folder_of_a_dot_velune_link_asks(ws):
    (ws / "cfgdir").mkdir()
    (ws / "cfgdir" / "hooks.json").write_text('{"hooks": {}}')
    _link_or_skip(ws / ".velune", ws / "cfgdir", is_dir=True)
    await _must_ask(ws, "move_path", source="cfgdir", destination="cfg_moved")
    await _must_ask(ws, "delete_directory", path="cfgdir", recursive=True)
    await _must_ask(ws, "delete_directory", path=".velune", recursive=True)


async def test_an_ordinary_looking_alias_of_dot_velune_asks(ws):
    (ws / ".velune").mkdir()
    (ws / ".velune" / "hooks.json").write_text('{"hooks": {}}')
    _link_or_skip(ws / "settings", ws / ".velune", is_dir=True)
    await _must_ask(ws, "write_file", file_path="settings/hooks.json", content=HOOKS)
    assert (ws / ".velune" / "hooks.json").read_text() == '{"hooks": {}}'


async def test_an_ordinary_looking_alias_of_git_internals_asks(ws):
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n")
    _link_or_skip(ws / "internals", ws / ".git", is_dir=True)
    await _must_ask(
        ws, "write_file", file_path="internals/config", content="[core]\n\tfsmonitor = x\n"
    )
    await _must_ask(ws, "write_file", file_path="internals/hooks/pre-commit", content="#!/bin/sh\n")


async def test_velune_toml_as_a_file_symlink_asks_in_both_directions(ws):
    (ws / "settings.toml").write_text("# config\n")
    _link_or_skip(ws / "velune.toml", ws / "settings.toml", is_dir=False)
    await _must_ask(ws, "write_file", file_path="velune.toml", content="[hooks]\n")
    await _must_ask(ws, "write_file", file_path="settings.toml", content="[hooks]\n")
    assert (ws / "settings.toml").read_text() == "# config\n"


async def test_a_file_symlink_that_points_at_the_hook_file_asks(ws):
    (ws / ".velune").mkdir()
    (ws / ".velune" / "hooks.json").write_text('{"hooks": {}}')
    _link_or_skip(ws / "innocent.json", ws / ".velune" / "hooks.json", is_dir=False)
    await _must_ask(ws, "write_file", file_path="innocent.json", content=HOOKS)


async def test_a_hard_link_to_the_hook_file_asks(ws):
    """Same file, different name: identity is compared, not just text."""
    (ws / ".velune").mkdir()
    (ws / ".velune" / "hooks.json").write_text('{"hooks": {}}')
    try:
        os.link(ws / ".velune" / "hooks.json", ws / "innocent.json")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable here")
    await _must_ask(ws, "write_file", file_path="innocent.json", content=HOOKS)


async def test_dotdot_after_a_link_cannot_hide_a_protected_name(ws, tmp_path):
    """``link/../.velune/hooks.json`` is collapsed lexically here but resolves elsewhere: judge both."""
    (ws / "sub").mkdir()
    (ws / "deep" / "er").mkdir(parents=True)
    _link_or_skip(ws / "sub" / "up", ws / "deep" / "er", is_dir=True)
    await _must_ask(ws, "write_file", file_path="sub/up/../.velune/hooks.json", content=HOOKS)


async def test_links_to_ordinary_places_are_not_blocked(ws):
    """Only links that involve a protected location change the answer."""
    (ws / "real").mkdir()
    (ws / "real" / "f.txt").write_text("x")
    _link_or_skip(ws / "alias", ws / "real", is_dir=True)
    await _must_not_ask(ws, "write_file", file_path="alias/g.txt", content="ok")
    assert (ws / "real" / "g.txt").read_text() == "ok"


async def test_an_extra_granted_root_keeps_its_protected_names(ws, tmp_path):
    other = tmp_path / "other"
    (other / ".velune").mkdir(parents=True)
    boundary = Boundary(ws)
    boundary.grant(other)
    hooks = other / ".velune" / "hooks.json"
    assert path_action(ActionType.MODIFY_FILE, hooks, boundary, "w").risk is Risk.HIGH


def test_a_broken_link_does_not_crash_classification(ws):
    try:
        (ws / "dangling").symlink_to(ws / "nowhere", target_is_directory=False)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable here")
    action = path_action(ActionType.MODIFY_FILE, "dangling", Boundary(ws), "w")
    assert action.risk in (Risk.LOW, Risk.MEDIUM, Risk.HIGH)
