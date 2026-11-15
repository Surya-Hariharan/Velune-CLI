"""Plugin manifests, command files and skill files are confined to the plugin (F-10e, F-10d).

A manifest names the folders and files it uses, and a repository can ship symlinks, so a path
in a plugin must never reach outside the plugin: a ``commands/x.md`` linked to a secret file
would be read and sent to the model provider as a prompt. Plugin names are identities (they
namespace MCP servers), so they are validated too, and one broken plugin must not cost the
others. Nothing here runs plugin code; hook and MCP configuration is only registered.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from velune.hooks.dispatcher import HookDispatcher
from velune.mcp.registry import MCPServerRegistry
from velune.plugins.declarative.manifest import PLUGIN_NAME_RE, DeclarativePluginManifest
from velune.plugins.declarative.paths import (
    PluginPathError,
    check_declared,
    is_within,
    resolve_within,
)
from velune.plugins.manager import PluginManager

HOOKS = {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo HOOK"}]}]}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def plugins_dir(workspace: Path) -> Path:
    return workspace / ".velune" / "plugins"


def make_plugin(
    parent: Path,
    dirname: str,
    *,
    name: str | None = None,
    manifest: dict | None = None,
    hooks: bool = False,
    mcp: dict | None = None,
    commands: dict | None = None,
) -> Path:
    root = parent / dirname
    (root / ".velune-plugin").mkdir(parents=True)
    body = {"name": dirname if name is None else name, "version": "1", **(manifest or {})}
    (root / ".velune-plugin" / "plugin.json").write_text(json.dumps(body))
    if hooks:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps(HOOKS))
    if mcp is not None:
        (root / ".mcp.json").write_text(json.dumps(mcp))
    for command, text in (commands or {}).items():
        (root / "commands").mkdir(exist_ok=True)
        data = text if isinstance(text, bytes) else text.encode()
        (root / "commands" / f"{command}.md").write_bytes(data)
    return root


def symlink(target: Path, link: Path) -> Path:
    """Link *link* to *target*. Without symlink privilege on Windows, a directory junction
    (which needs none and resolves the same way) stands in for a directory symlink."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        if sys.platform == "win32" and target.is_dir():
            done = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
            )
            if done.returncode == 0 and link.exists():
                return link
        pytest.skip("symlinks are not available here")
    return link


def trusted_manager(workspace: Path) -> PluginManager:
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    manager.load()
    return manager


# ── the declared text ─────────────────────────────────────────────────────────────────────────

REJECTED = [
    "../x",
    "a/../../x",
    "..\\x",
    "a\\..\\..\\x",
    "a/..\\b",  # mixed separators
    "..",
    "/abs/path",
    "\\rooted",
    "C:\\Windows\\x",
    "C:/Windows/x",
    "C:relative",  # drive-relative
    "\\\\host\\share\\x",  # UNC
    "//host/share/x",
    "",
    "   ",
    "a\x00b",
]
ACCEPTED = ["commands", "hooks/hooks.json", "a/b/c", ".mcp.json", "a..b", "..hidden", "dir.d/file"]


@pytest.mark.parametrize("declared", REJECTED)
def test_check_declared_rejects_on_every_platform(declared):
    with pytest.raises(PluginPathError):
        check_declared(declared)


@pytest.mark.parametrize("declared", [None, 5, ["a"], b"commands"])
def test_check_declared_rejects_non_text(declared):
    with pytest.raises(PluginPathError):
        check_declared(declared)


@pytest.mark.parametrize("declared", ACCEPTED)
def test_check_declared_accepts_plain_relative_paths(declared):
    assert check_declared(declared) == declared


# ── where the path really is ──────────────────────────────────────────────────────────────────


def test_a_nested_valid_path_resolves_inside_the_plugin(tmp_path):
    root = tmp_path / "plugin"
    (root / "a" / "b").mkdir(parents=True)
    assert resolve_within(root, "a/b") == (root / "a" / "b").resolve()


def test_a_missing_target_is_allowed_when_it_would_be_inside(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    assert resolve_within(root, "hooks/hooks.json") == (root / "hooks" / "hooks.json").resolve()


def test_containment_compares_path_components_not_string_prefixes(tmp_path):
    root = tmp_path / "plugin"
    sibling = tmp_path / "plugin-evil"
    root.mkdir()
    sibling.mkdir()
    assert is_within(root / "x", root)
    assert is_within(root, root)
    assert not is_within(sibling, root)  # "…/plugin-evil".startswith("…/plugin") is the trap
    assert not is_within(tmp_path, root)


def test_a_symlink_that_leaves_the_plugin_is_outside(tmp_path):
    root = tmp_path / "plugin"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = symlink(outside, root / "link")
    assert not is_within(link, root)
    with pytest.raises(PluginPathError):
        resolve_within(root, "link/file.json")


def test_a_symlink_that_stays_inside_the_plugin_is_fine(tmp_path):
    root = tmp_path / "plugin"
    (root / "real").mkdir(parents=True)
    link = symlink(root / "real", root / "alias")
    assert is_within(link, root)


# ── a manifest that declares an escaping path ─────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["commands", "skills", "hooks", "mcpServers"])
@pytest.mark.parametrize(
    "value", ["../outside", "/etc/passwd", "C:\\Windows", "\\\\host\\share", "a\\..\\..\\x"]
)
def test_a_manifest_declaring_an_escaping_path_is_rejected(tmp_path, caplog, field, value):
    root = make_plugin(tmp_path, "esc", manifest={field: value})
    with caplog.at_level(logging.WARNING, logger="velune.plugins.declarative.manifest"):
        assert DeclarativePluginManifest.load(root) is None
    assert "Malformed plugin manifest" in caplog.text


def test_a_plugin_with_one_escaping_path_registers_nothing_at_all(workspace, home):
    """No partial registration: its valid commands, hooks and servers are not used either."""
    make_plugin(
        plugins_dir(workspace),
        "esc",
        manifest={"skills": "../elsewhere"},  # the bad entry
        hooks=True,
        mcp={"s": {"command": "echo"}},
        commands={"ok": "fine"},
    )
    manager = trusted_manager(workspace)
    dispatcher = HookDispatcher(workspace=workspace, trusted=True)
    registry = MCPServerRegistry(workspace=workspace)

    assert manager.get_plugin("esc") is None
    assert manager.all_commands() == []
    assert manager.wire_hooks(dispatcher) == 0 and manager.wire_mcp(registry) == 0
    assert registry._entries == {}


def test_several_invalid_entries_are_all_rejected_not_just_the_first(tmp_path):
    root = make_plugin(tmp_path, "esc", manifest={"hooks": "/abs", "commands": "../x"})
    assert DeclarativePluginManifest.load(root) is None


def test_the_path_is_checked_again_where_it_is_used(workspace, home, caplog):
    """A manifest that was valid when loaded cannot be used for a path it no longer allows."""
    make_plugin(plugins_dir(workspace), "late", hooks=True)
    manager = trusted_manager(workspace)
    plugin = manager.get_plugin("late")
    assert plugin is not None
    plugin.manifest.hooks_path = "../../../elsewhere.json"  # as if changed after loading
    with pytest.raises(PluginPathError):
        _ = plugin.manifest.hooks_file
    dispatcher = HookDispatcher(workspace=workspace, trusted=True)
    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        assert manager.wire_hooks(dispatcher) == 0  # refused, not crashed
    assert [h.command for b in dispatcher._ensure_loaded() for h in b.hooks] == []


# ── symlinks shipped by a repository ──────────────────────────────────────────────────────────


def test_a_command_file_linked_to_a_secret_is_never_read(workspace, home, tmp_path, caplog):
    secret = tmp_path / "secret.md"
    secret.write_text("TOP-SECRET-CONTENT")
    root = make_plugin(plugins_dir(workspace), "linky", commands={"real": "real body"})
    symlink(secret, root / "commands" / "stolen.md")

    with caplog.at_level(logging.WARNING, logger="velune.plugins.declarative.command"):
        manager = trusted_manager(workspace)

    names = [c.name for c in manager.all_commands()]
    assert names == ["real"]
    assert "TOP-SECRET-CONTENT" not in "".join(c.body for c in manager.all_commands())
    assert "resolves outside the plugin" in caplog.text


def test_command_files_outside_the_plugin_root_are_skipped_whatever_put_them_there(
    tmp_path, caplog
):
    """The per-file guard on its own, so it is exercised even where symlinks cannot be made."""
    from velune.plugins.declarative.command import load_plugin_commands

    plugin_root = tmp_path / "plugin"
    elsewhere = tmp_path / "elsewhere"
    plugin_root.mkdir()
    elsewhere.mkdir()
    (elsewhere / "stolen.md").write_text("TOP-SECRET-CONTENT")

    with caplog.at_level(logging.WARNING, logger="velune.plugins.declarative.command"):
        confined = load_plugin_commands(elsewhere, "p", plugin_root=plugin_root)
    unconfined = load_plugin_commands(elsewhere, "p")  # no root given: nothing to confine to

    assert confined == []
    assert "resolves outside the plugin" in caplog.text
    assert [c.name for c in unconfined] == ["stolen"]  # proves the file was readable


def test_a_commands_folder_linked_outside_skips_that_plugin_only(workspace, home, tmp_path):
    outside = tmp_path / "outside-commands"
    outside.mkdir()
    (outside / "evil.md").write_text("EVIL")
    root = make_plugin(plugins_dir(workspace), "linked")
    symlink(outside, root / "commands")
    make_plugin(home / ".velune" / "plugins", "mine", commands={"mine-cmd": "mine"})

    manager = trusted_manager(workspace)

    assert manager.get_plugin("linked") is None
    assert [c.name for c in manager.all_commands()] == ["mine-cmd"]  # the others still load


def test_a_skill_folder_linked_outside_is_skipped(workspace, home, tmp_path):
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nalways: true\n---\nINJECT")
    root = make_plugin(plugins_dir(workspace), "skilly")
    (root / "skills").mkdir()
    symlink(outside, root / "skills" / "bad")

    manager = trusted_manager(workspace)

    assert manager.get_plugin("skilly") is not None
    assert manager.all_skills() == []


# ── malformed and unusual content ─────────────────────────────────────────────────────────────


def test_a_command_file_that_is_not_utf8_skips_that_command_only(workspace, home, caplog):
    make_plugin(
        plugins_dir(workspace),
        "mixed",
        commands={"good": "fine", "bad": b"\xff\xfe\x00 not utf-8 \xff"},
    )
    with caplog.at_level(logging.WARNING, logger="velune.plugins.declarative.command"):
        manager = trusted_manager(workspace)
    assert [c.name for c in manager.all_commands()] == ["good"]
    assert "Cannot read command file" in caplog.text


def test_one_unreadable_plugin_does_not_cost_the_others(workspace, home, monkeypatch):
    from velune.plugins.declarative import scanner as scanner_module

    make_plugin(home / ".velune" / "plugins", "mine", commands={"mine-cmd": "m"})
    make_plugin(plugins_dir(workspace), "boom", commands={"boom-cmd": "b"})
    real = scanner_module.load_plugin_commands

    def flaky(commands_dir, plugin_name, plugin_root=None):
        if plugin_name == "boom":
            raise OSError("simulated read failure")
        return real(commands_dir, plugin_name, plugin_root=plugin_root)

    monkeypatch.setattr(scanner_module, "load_plugin_commands", flaky)
    manager = trusted_manager(workspace)

    assert manager.get_plugin("boom") is None
    assert [c.name for c in manager.all_commands()] == ["mine-cmd"]


def test_a_hooks_file_that_is_a_directory_or_malformed_registers_nothing(workspace, home):
    root = make_plugin(plugins_dir(workspace), "weird", hooks=True)
    (root / "hooks" / "hooks.json").unlink()
    (root / "hooks" / "hooks.json").mkdir()  # a directory where a file belongs
    make_plugin(plugins_dir(workspace), "broken-json")
    (plugins_dir(workspace) / "broken-json" / "hooks").mkdir()
    (plugins_dir(workspace) / "broken-json" / "hooks" / "hooks.json").write_text("{not json")

    manager = trusted_manager(workspace)
    dispatcher = HookDispatcher(workspace=workspace, trusted=True)

    assert manager.wire_hooks(dispatcher) == 0
    assert [h.command for b in dispatcher._ensure_loaded() for h in b.hooks] == []


def test_a_missing_optional_file_is_not_an_error(workspace, home):
    make_plugin(plugins_dir(workspace), "bare")
    manager = trusted_manager(workspace)
    plugin = manager.get_plugin("bare")
    assert plugin is not None and not plugin.has_hooks and not plugin.has_mcp


# ── plugin identity ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["good", "a.b", "a_b", "a-b", "A1", "x" * 64, "0day"])
def test_valid_plugin_names(name):
    assert PLUGIN_NAME_RE.fullmatch(name)


@pytest.mark.parametrize(
    "name",
    ["", " ", "a:b", "a b", "a/b", "a\\b", "-lead", ".lead", "x" * 65, "а", "a\nb", "a\x00b"],
)
def test_invalid_plugin_names_are_rejected_when_the_manifest_loads(tmp_path, name):
    root = make_plugin(tmp_path, "dir-name", name=name)
    assert DeclarativePluginManifest.load(root) is None


def test_two_plugins_cannot_collide_on_a_qualified_mcp_name(workspace, home):
    """Plugin ``a`` + server ``b:c`` and plugin ``a:b`` + server ``c`` both used to become
    ``a:b:c``, so one silently replaced the other."""
    make_plugin(plugins_dir(workspace), "a-dir", name="a", mcp={"b:c": {"command": "echo"}})
    make_plugin(plugins_dir(workspace), "ab-dir", name="a:b", mcp={"c": {"command": "echo"}})
    manager = trusted_manager(workspace)
    registry = MCPServerRegistry(workspace=workspace)

    assert sorted(p.name for p in manager._active_plugins()) == ["a"]  # "a:b" was rejected
    manager.wire_mcp(registry)
    assert list(registry._entries) == ["a:b:c"]  # exactly one, owned by plugin "a"
