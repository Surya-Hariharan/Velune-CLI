"""Disabling, reloading and re-enabling a plugin really revoke and restore it (F-10b, F-10d).

``/plugin disable`` used to flip a flag and nothing else: the plugin's hooks kept firing, its
MCP servers stayed connected and callable, and its slash commands stayed registered. Reload
re-registered commands only and wiring twice duplicated hooks. Everything a plugin registers is
now owned by the plugin, so it can be removed, replaced and restored as a unit, and one plugin's
lifecycle cannot touch another's.

No plugin code runs: the hook subprocess runner is a recorder and MCP connections are fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.cli.handlers.plugins import register_plugin_commands
from velune.cli.slash_commands import SlashCommandRegistry
from velune.hooks import dispatcher as hook_dispatcher_module
from velune.hooks.dispatcher import HookDispatcher
from velune.hooks.types import HookResult
from velune.mcp.registry import MCPServerRegistry, ServerState
from velune.mcp.transports.base import MCPTransportError, ServerConfig, ToolInfo
from velune.plugins.declarative.scanner import ORIGIN_USER, ORIGIN_WORKSPACE, PluginScanner
from velune.plugins.manager import PluginManager


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


def hooks_json(command: str) -> dict:
    return {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}


def make_plugin(
    parent: Path,
    name: str,
    *,
    hook: str | None = None,
    servers: dict | None = None,
    commands: dict | None = None,
) -> Path:
    root = parent / name
    (root / ".velune-plugin").mkdir(parents=True)
    (root / ".velune-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": "1"}))
    if hook is not None:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps(hooks_json(hook)))
    if servers is not None:
        (root / ".mcp.json").write_text(json.dumps(servers))
    for command, body in (commands or {}).items():
        (root / "commands").mkdir(exist_ok=True)
        (root / "commands" / f"{command}.md").write_text(body)
    return root


def ws_plugins(workspace: Path) -> Path:
    return workspace / ".velune" / "plugins"


class FakeConnection:
    def __init__(self, fail_on_close: bool = False) -> None:
        self.closed = False
        self.fail_on_close = fail_on_close

    async def disconnect(self) -> None:
        self.closed = True
        if self.fail_on_close:
            raise RuntimeError("simulated close failure")

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        return f"ran {tool_name}"


def connect_fake(registry: MCPServerRegistry, name: str, **kwargs) -> FakeConnection:
    """Mark a registered server as connected, with one tool, without any real connection."""
    entry = registry._entries[name]
    connection = FakeConnection(**kwargs)
    entry.connection = connection  # type: ignore[assignment]
    entry.state = ServerState.CONNECTED
    entry.tools = [ToolInfo(name="tool", description="d", input_schema={}, server_name=name)]
    registry._rebuild_tool_index()
    return connection


class Session:
    """A manager wired into real registries, the way the REPL wires it."""

    def __init__(self, workspace: Path, *, trusted: bool = True) -> None:
        self.manager = PluginManager(workspace=workspace, workspace_trusted=trusted)
        self.dispatcher = HookDispatcher(workspace=workspace, trusted=trusted)
        self.mcp = MCPServerRegistry(workspace=workspace)
        self.commands = SlashCommandRegistry()
        self.commands.seal_builtins()
        self.repl = MagicMock()
        self.repl._registry = self.commands
        self.repl._completer = None
        self.repl._command_palette = None
        self.repl._plugin_manager = self.manager
        self.repl._handle_prompt = AsyncMock()
        self.repl.console = MagicMock()

    def start(self) -> Session:
        self.manager.attach_commands(
            self.commands, lambda plugins: register_plugin_commands(self.repl, plugins)
        )
        found = self.manager.load()
        register_plugin_commands(self.repl, found)
        self.manager.wire_hooks(self.dispatcher)
        self.manager.wire_mcp(self.mcp)
        return self

    def hook_commands(self) -> list[str]:
        return [h.command for b in self.dispatcher._ensure_loaded() for h in b.hooks]


@pytest.fixture
def hook_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every hook batch the dispatcher would run, instead of running anything."""
    runs: list[list[str]] = []

    async def record(definitions, payload, workspace=None):
        runs.append([d.command for d in definitions])
        return HookResult()

    monkeypatch.setattr(hook_dispatcher_module, "run_hooks_parallel", record)
    return runs


# ── disable ───────────────────────────────────────────────────────────────────────────────────


async def test_disable_revokes_hooks_commands_and_servers(workspace, home, hook_runs):
    make_plugin(
        ws_plugins(workspace),
        "p1",
        hook="echo HOOK",
        servers={"s": {"command": "echo"}},
        commands={"review": "BODY"},
    )
    session = Session(workspace).start()
    connection = connect_fake(session.mcp, "p1:s")
    handler = session.commands.get("review").handler  # a reference someone might hold on to

    await session.dispatcher.dispatch_pre_tool_use("Bash", {})
    assert hook_runs == [["echo HOOK"]]  # it fires while enabled
    assert await session.mcp.call_tool("p1:s_tool", {}) == "ran tool"

    assert session.manager.disable("p1") is True
    await session.manager.flush()

    hook_runs.clear()
    await session.dispatcher.dispatch_pre_tool_use("Bash", {})
    assert hook_runs == []  # no longer fires
    assert session.dispatcher.has_hooks is False
    assert session.commands.get("review") is None
    assert "p1:s" not in session.mcp._entries
    with pytest.raises(MCPTransportError):
        await session.mcp.call_tool("p1:s_tool", {})  # no longer callable
    assert session.mcp.all_tools() == []
    assert connection.closed is True  # the process was actually closed
    assert session.manager.is_active("p1") is False
    assert session.manager.all_commands() == []

    await handler("")  # a stale reference to the command refuses to run
    session.repl._handle_prompt.assert_not_awaited()


async def test_disable_closes_servers_in_the_background_when_a_loop_is_running(workspace, home):
    make_plugin(ws_plugins(workspace), "p1", servers={"s": {"command": "echo"}})
    session = Session(workspace).start()
    connection = connect_fake(session.mcp, "p1:s")

    session.manager.disable("p1")  # not awaiting flush(): a task was scheduled
    for _ in range(50):
        if connection.closed:
            break
        await asyncio.sleep(0.01)

    assert connection.closed is True


def test_disable_works_without_a_running_loop_and_flush_closes_later(workspace, home):
    make_plugin(ws_plugins(workspace), "p1", servers={"s": {"command": "echo"}})
    session = Session(workspace).start()
    connection = connect_fake(session.mcp, "p1:s")

    session.manager.disable("p1")  # plain sync call, as from a direct API user

    assert "p1:s" not in session.mcp._entries  # already unreachable
    assert connection.closed is False
    asyncio.run(session.manager.flush())
    assert connection.closed is True


async def test_disabling_unknown_plugins_and_repeating_are_harmless(workspace, home):
    make_plugin(ws_plugins(workspace), "p1", hook="echo H")
    session = Session(workspace).start()
    assert session.manager.disable("nope") is False
    assert session.manager.enable("nope") is False
    session.manager.disable("p1")
    session.manager.disable("p1")
    assert session.manager.revoke("p1") == {"hooks": 0, "commands": 0, "servers": 0}
    await session.manager.flush()
    await session.manager.flush()


# ── enable ────────────────────────────────────────────────────────────────────────────────────


async def test_enable_restores_exactly_what_the_plugin_provides(workspace, home, hook_runs):
    make_plugin(
        ws_plugins(workspace),
        "p1",
        hook="echo ONE",
        servers={"s": {"command": "echo"}},
        commands={"review": "BODY"},
    )
    make_plugin(ws_plugins(workspace), "p2", hook="echo TWO")
    session = Session(workspace).start()
    session.manager.disable("p1")
    await session.manager.flush()
    assert session.hook_commands() == ["echo TWO"]

    assert session.manager.enable("p1") is True

    assert sorted(session.hook_commands()) == ["echo ONE", "echo TWO"]
    assert list(session.mcp._entries) == ["p1:s"]
    assert session.mcp._entries["p1:s"].owner == "p1"
    assert session.commands.get("review") is not None
    assert session.manager.is_active("p1")


async def test_a_disabled_plugin_stays_disabled_across_reload(workspace, home):
    make_plugin(ws_plugins(workspace), "p1", hook="echo H", commands={"review": "B"})
    session = Session(workspace).start()
    session.manager.disable("p1")

    session.manager.reload()
    await session.manager.flush()

    assert session.hook_commands() == []
    assert session.commands.get("review") is None
    assert session.manager.is_active("p1") is False


# ── reload ────────────────────────────────────────────────────────────────────────────────────


async def test_repeated_reloads_never_accumulate_registrations(workspace, home):
    make_plugin(
        ws_plugins(workspace),
        "p1",
        hook="echo H",
        servers={"a": {"command": "echo"}, "b": {"command": "echo"}},
        commands={"review": "B", "other": "B"},
    )
    session = Session(workspace).start()
    expected_commands = sorted(session.commands._commands)

    for _ in range(5):
        found = session.manager.reload()
        await session.manager.flush()
        assert [p.name for p in found] == ["p1"]

    assert session.hook_commands() == ["echo H"]
    assert sorted(session.mcp._entries) == ["p1:a", "p1:b"]
    assert sorted(session.commands._commands) == expected_commands


async def test_wiring_the_same_plugin_twice_does_not_duplicate_hooks_or_disturb_servers(
    workspace, home
):
    make_plugin(ws_plugins(workspace), "p1", hook="echo H", servers={"s": {"command": "echo"}})
    session = Session(workspace).start()
    connection = connect_fake(session.mcp, "p1:s")

    session.manager.wire_hooks(session.dispatcher)
    session.manager.wire_hooks(session.dispatcher)
    session.manager.wire_mcp(session.mcp)
    session.manager.wire_mcp(session.mcp)
    await session.manager.flush()

    assert session.hook_commands() == ["echo H"]
    assert session.mcp._entries["p1:s"].is_connected  # an unchanged server is left alone
    assert connection.closed is False


async def test_reloading_one_plugin_leaves_the_others_untouched(workspace, home):
    make_plugin(ws_plugins(workspace), "p1", hook="echo ONE", servers={"s": {"command": "echo"}})
    make_plugin(ws_plugins(workspace), "p2", hook="echo TWO", servers={"s": {"command": "echo"}})
    session = Session(workspace).start()
    keep = connect_fake(session.mcp, "p2:s")

    session.manager.reload("p1")
    await session.manager.flush()

    assert sorted(session.hook_commands()) == ["echo ONE", "echo TWO"]
    assert session.mcp._entries["p2:s"].is_connected and keep.closed is False
    assert not session.mcp._entries["p1:s"].is_connected  # re-registered, to be connected anew


async def test_a_plugin_that_becomes_invalid_on_reload_loses_what_it_had(workspace, home):
    root = make_plugin(
        ws_plugins(workspace), "p1", hook="echo H", servers={"s": {"command": "echo"}}
    )
    session = Session(workspace).start()
    assert session.hook_commands() == ["echo H"]

    (root / "hooks" / "hooks.json").write_text("{not json")  # broken on disk
    (root / ".mcp.json").write_text(json.dumps({"s": {"command": "echo", "timeout": "x"}}))
    session.manager.reload("p1")
    await session.manager.flush()

    assert session.hook_commands() == []  # fails closed, does not keep the old hooks
    assert session.mcp._entries == {}


async def test_reload_removes_registrations_the_plugin_no_longer_declares(workspace, home):
    """The point of revoking first: what a plugin dropped must not linger after a reload."""
    root = make_plugin(
        ws_plugins(workspace),
        "p1",
        hook="echo H",
        servers={"keep": {"command": "echo"}, "gone": {"command": "echo"}},
        commands={"keep-cmd": "K", "gone-cmd": "G"},
    )
    session = Session(workspace).start()
    gone = connect_fake(session.mcp, "p1:gone")
    assert sorted(session.mcp._entries) == ["p1:gone", "p1:keep"]

    # The author removes a server, a command and the hooks, then the user reloads.
    (root / ".mcp.json").write_text(json.dumps({"keep": {"command": "echo"}}))
    (root / "commands" / "gone-cmd.md").unlink()
    (root / "hooks" / "hooks.json").unlink()
    session.manager.reload("p1")
    await session.manager.flush()

    assert sorted(session.mcp._entries) == ["p1:keep"]
    assert gone.closed is True  # the dropped server's process was closed
    assert session.commands.get("gone-cmd") is None
    assert session.commands.get("keep-cmd") is not None
    assert session.hook_commands() == []


async def test_reload_of_a_plugin_whose_folder_was_deleted_removes_everything_it_had(
    workspace, home
):
    import shutil

    root = make_plugin(
        ws_plugins(workspace),
        "p1",
        hook="echo H",
        servers={"s": {"command": "echo"}},
        commands={"cmd": "C"},
    )
    session = Session(workspace).start()
    connection = connect_fake(session.mcp, "p1:s")

    shutil.rmtree(root)
    assert session.manager.reload("p1") == []
    await session.manager.flush()

    assert session.hook_commands() == []
    assert session.mcp._entries == {} and connection.closed is True
    assert session.commands.get("cmd") is None
    assert session.manager.get_plugin("p1") is None


# ── partial failure ───────────────────────────────────────────────────────────────────────────


async def test_a_bad_server_entry_registers_none_of_the_plugins_servers(workspace, home, caplog):
    make_plugin(
        ws_plugins(workspace),
        "p1",
        servers={
            "good": {"command": "echo"},
            "bad": {"command": "echo", "timeout": "not-a-number"},
        },
    )
    session = Session(workspace)
    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        session.start()

    assert session.mcp._entries == {}
    assert "Failed to wire MCP from plugin 'p1'" in caplog.text


async def test_a_bad_plugin_does_not_stop_the_good_ones(workspace, home):
    make_plugin(ws_plugins(workspace), "bad", servers={"x": {"command": "echo", "timeout": "no"}})
    make_plugin(ws_plugins(workspace), "good", servers={"y": {"command": "echo"}})
    session = Session(workspace).start()
    assert list(session.mcp._entries) == ["good:y"]


# ── cleanup failures ──────────────────────────────────────────────────────────────────────────


async def test_a_server_that_fails_to_close_does_not_strand_the_others(workspace, home, caplog):
    make_plugin(
        ws_plugins(workspace),
        "p1",
        servers={"a": {"command": "echo"}, "b": {"command": "echo"}},
    )
    session = Session(workspace).start()
    failing = connect_fake(session.mcp, "p1:a", fail_on_close=True)
    other = connect_fake(session.mcp, "p1:b")

    session.manager.disable("p1")
    with caplog.at_level(logging.WARNING, logger="velune.mcp.registry"):
        await session.manager.flush()  # must not raise

    assert failing.closed and other.closed
    assert session.mcp._entries == {}
    assert "Error closing MCP server" in caplog.text
    await session.manager.flush()  # idempotent: nothing left to close


# ── two plugins ───────────────────────────────────────────────────────────────────────────────


async def test_disabling_one_plugin_does_not_remove_the_overlapping_registrations_of_another(
    workspace, home, hook_runs
):
    make_plugin(
        ws_plugins(workspace),
        "alpha",
        hook="echo SAME",
        servers={"srv": {"command": "echo"}},
        commands={"deploy": "A", "only-alpha": "A"},
    )
    make_plugin(
        ws_plugins(workspace),
        "beta",
        hook="echo SAME",  # identical hook command
        servers={"srv": {"command": "echo"}},  # same short server name
        commands={"deploy": "B", "only-beta": "B"},  # same command name
    )
    session = Session(workspace).start()
    beta_connection = connect_fake(session.mcp, "beta:srv")
    assert session.commands.get("deploy").owner == "alpha"  # first registered wins

    session.manager.disable("alpha")
    await session.manager.flush()

    assert session.hook_commands() == ["echo SAME"]  # beta's copy remains, alpha's is gone
    assert list(session.mcp._entries) == ["beta:srv"]
    assert session.mcp._entries["beta:srv"].is_connected and beta_connection.closed is False
    assert session.commands.get("only-beta") is not None
    assert session.commands.get("only-alpha") is None
    await session.dispatcher.dispatch_pre_tool_use("Bash", {})
    assert hook_runs == [["echo SAME"]]


# ── the dispatcher and the registry on their own ──────────────────────────────────────────────


def test_invalidating_the_hook_cache_keeps_plugin_hooks(workspace, home):
    make_plugin(ws_plugins(workspace), "p1", hook="echo H")
    session = Session(workspace).start()
    assert session.hook_commands() == ["echo H"]

    session.dispatcher.invalidate_cache()
    session.dispatcher.set_trusted(False)  # also invalidates

    assert session.hook_commands() == ["echo H"]  # used to be dropped silently


def test_plugin_bindings_are_replaced_and_removed_by_owner():
    dispatcher = HookDispatcher()
    binding = SimpleNamespace(hooks=[SimpleNamespace(command="x")])
    dispatcher.add_plugin_bindings("a", [binding])  # type: ignore[list-item]
    dispatcher.add_plugin_bindings("a", [binding])  # type: ignore[list-item]
    assert len(dispatcher._plugin_bindings["a"]) == 1
    assert dispatcher.remove_plugin_bindings("a") is True
    assert dispatcher.remove_plugin_bindings("a") is False


def test_the_mcp_registry_never_lets_one_owner_take_anothers_server(caplog):
    registry = MCPServerRegistry()
    config = ServerConfig.from_dict("shared", {"command": "echo"})
    registry.register(config)  # the user's own server
    with caplog.at_level(logging.WARNING, logger="velune.mcp.registry"):
        registry.register(ServerConfig.from_dict("shared", {"command": "evil"}), owner="p")
    assert registry._entries["shared"].config.command == "echo"
    assert registry._entries["shared"].owner == ""
    assert "already registered by the user's config" in caplog.text

    registry.register(ServerConfig.from_dict("p:srv", {"command": "echo"}), owner="p")
    registry.register(ServerConfig.from_dict("p:srv", {"command": "evil"}), owner="q")
    assert registry._entries["p:srv"].config.command == "echo"

    assert [e.name for e in registry.detach_owner("p")] == ["p:srv"]
    assert registry.detach_owner("p") == []
    assert registry.owned_entries("") == {}  # the user's servers are never "owned"
    assert "shared" in registry._entries


# ── trust revoked ─────────────────────────────────────────────────────────────────────────────


async def test_losing_workspace_trust_revokes_workspace_plugins_but_not_user_plugins(
    workspace, home, hook_runs
):
    make_plugin(
        ws_plugins(workspace),
        "theirs",
        hook="echo THEIRS",
        servers={"s": {"command": "echo"}},
        commands={"their-cmd": "T"},
    )
    make_plugin(home / ".velune" / "plugins", "mine", hook="echo MINE", commands={"my-cmd": "M"})
    session = Session(workspace, trusted=True).start()
    theirs = connect_fake(session.mcp, "theirs:s")
    assert sorted(session.hook_commands()) == ["echo MINE", "echo THEIRS"]

    session.manager.set_workspace_trusted(False)
    await session.manager.flush()

    assert session.hook_commands() == ["echo MINE"]
    assert session.commands.get("their-cmd") is None
    assert session.commands.get("my-cmd") is not None
    assert session.mcp._entries == {} and theirs.closed is True
    await session.dispatcher.dispatch_pre_tool_use("Bash", {})
    assert hook_runs == [["echo MINE"]]


# ── name clashes (F-10d) ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("workspace_first", [True, False])
def test_a_user_plugin_beats_a_workspace_plugin_of_the_same_name_in_either_order(
    workspace, home, caplog, workspace_first
):
    make_plugin(home / ".velune" / "plugins", "shared", commands={"cmd": "USER"})
    make_plugin(ws_plugins(workspace), "shared", commands={"cmd": "WORKSPACE"})
    scanner = PluginScanner()
    paths = [
        (ws_plugins(workspace), ORIGIN_WORKSPACE),
        (home / ".velune" / "plugins", ORIGIN_USER),
    ]
    for path, origin in paths if workspace_first else reversed(paths):
        scanner.add_path(path, origin=origin)

    with caplog.at_level(logging.WARNING, logger="velune.plugins.declarative.scanner"):
        found = scanner.scan(workspace_trusted=True)

    assert [(p.name, p.origin) for p in found] == [("shared", ORIGIN_USER)]
    assert found[0].commands[0].body == "USER"
    assert "was skipped: a plugin of that name from user is used instead" in caplog.text


def test_in_a_trusted_workspace_the_manager_uses_the_user_plugin(workspace, home):
    make_plugin(home / ".velune" / "plugins", "shared", commands={"cmd": "USER"})
    make_plugin(ws_plugins(workspace), "shared", commands={"cmd": "WORKSPACE"})
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    manager.load()
    assert manager.find_command("cmd").body == "USER"
    assert manager.plugin_count == 1


def test_a_workspace_plugin_with_a_unique_name_is_unaffected_by_the_name_rule(workspace, home):
    make_plugin(home / ".velune" / "plugins", "mine", commands={"a": "x"})
    make_plugin(ws_plugins(workspace), "theirs", commands={"b": "y"})
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    assert sorted(p.name for p in manager.load()) == ["mine", "theirs"]
