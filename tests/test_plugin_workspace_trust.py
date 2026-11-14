"""F-10a: a workspace's plugins are loaded only for a workspace the user has trusted.

``<workspace>/.velune/plugins`` is project-controlled content. A plugin's hooks run shell
commands and its ``.mcp.json`` starts local processes, so a cloned repository could run
code the first time Velune opened it -- with no prompt, because the trust prompt did not
know that folder existed. These tests pin the gate at every layer: the scanner, the
manager's wiring methods, the trust prompt, and the real ``VeluneREPL.run()`` start-up
order. External execution (MCP connections, hook subprocesses) is always mocked: no
plugin code ever runs here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.cli.handlers.plugins import load_and_register_plugins
from velune.cli.repl import VeluneREPL
from velune.cli.slash_commands import SlashCommandRegistry
from velune.core import trust
from velune.hooks.dispatcher import HookDispatcher
from velune.mcp.registry import MCPServerRegistry
from velune.plugins.declarative.manifest import DeclarativePluginManifest
from velune.plugins.declarative.scanner import (
    ORIGIN_EXTRA,
    ORIGIN_USER,
    ORIGIN_WORKSPACE,
    DiscoveredPlugin,
    PluginScanner,
    has_workspace_plugins,
)
from velune.plugins.manager import PluginManager

HOOKS = {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo HOOK"}]}]}
MCP = {"srv": {"command": "echo", "args": ["server"]}}


# ── fixtures and builders ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake HOME, so a real user's plugins, hooks and .mcp.json are never read."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    return path


@pytest.fixture
def trust_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "trusted_dirs.json"
    monkeypatch.setattr(trust, "_trust_file", lambda: path)
    monkeypatch.delenv("VELUNE_TRUST_ALL", raising=False)
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def make_plugin(
    plugins_dir: Path,
    name: str,
    *,
    hooks: bool = True,
    mcp: bool = True,
    command: str | None = "review",
    flat: bool = False,
) -> Path:
    root = plugins_dir / name
    manifest_dir = root if flat else root / ".velune-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": name, "version": "1"}))
    if hooks:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps(HOOKS))
    if mcp:
        (root / ".mcp.json").write_text(json.dumps(MCP))
    if command:
        (root / "commands").mkdir()
        (root / "commands" / f"{command}.md").write_text(f"BODY OF {name}")
    return root


def workspace_plugins(workspace: Path) -> Path:
    return workspace / ".velune" / "plugins"


def user_plugins(home: Path) -> Path:
    return home / ".velune" / "plugins"


def hook_commands(dispatcher: HookDispatcher) -> list[str]:
    return [h.command for binding in dispatcher._ensure_loaded() for h in binding.hooks]


# ── scanner ───────────────────────────────────────────────────────────────────────────────────


def test_a_scan_skips_the_workspace_unless_it_is_told_it_is_trusted(workspace, home):
    make_plugin(workspace_plugins(workspace), "evil")
    scanner = PluginScanner.default_paths(workspace)

    assert scanner.scan() == []  # the default fails closed
    assert scanner.scan(workspace_trusted=False) == []
    assert [p.name for p in scanner.scan(workspace_trusted=True)] == ["evil"]


def test_an_untrusted_workspace_plugin_is_never_parsed(workspace, home, monkeypatch):
    make_plugin(workspace_plugins(workspace), "evil")
    make_plugin(user_plugins(home), "mine")
    parsed: list[Path] = []
    real_load = DeclarativePluginManifest.load.__func__

    def spy(cls, plugin_root):
        parsed.append(Path(plugin_root))
        return real_load(cls, plugin_root)

    monkeypatch.setattr(DeclarativePluginManifest, "load", classmethod(spy))
    PluginScanner.default_paths(workspace).scan(workspace_trusted=False)

    assert [p.name for p in parsed] == ["mine"]  # the repository's plugin was not even read


def test_each_plugin_records_where_it_came_from(workspace, home, tmp_path):
    make_plugin(workspace_plugins(workspace), "from-repo")
    make_plugin(user_plugins(home), "from-user")
    extra = tmp_path / "extra"
    make_plugin(extra, "from-extra")
    scanner = PluginScanner.default_paths(workspace)
    scanner.add_path(extra)

    origins = {p.name: p.origin for p in scanner.scan(workspace_trusted=True)}

    assert origins == {
        "from-repo": ORIGIN_WORKSPACE,
        "from-user": ORIGIN_USER,
        "from-extra": ORIGIN_EXTRA,
    }


def test_a_plugin_built_without_an_origin_is_treated_as_a_workspace_plugin(tmp_path):
    make_plugin(tmp_path, "anon")
    manifest = DeclarativePluginManifest.load(tmp_path / "anon")
    assert DiscoveredPlugin(manifest=manifest).origin == ORIGIN_WORKSPACE


def test_has_workspace_plugins(workspace):
    assert not has_workspace_plugins(workspace)  # no folder
    workspace_plugins(workspace).mkdir(parents=True)
    assert not has_workspace_plugins(workspace)  # empty folder
    (workspace_plugins(workspace) / "notes").mkdir()
    assert not has_workspace_plugins(workspace)  # a folder that is not a plugin
    make_plugin(workspace_plugins(workspace), "flat", flat=True)
    assert has_workspace_plugins(workspace)  # flat plugin.json layout


def test_has_workspace_plugins_sees_the_preferred_layout(workspace):
    make_plugin(workspace_plugins(workspace), "p")
    assert has_workspace_plugins(workspace)


# ── manager: an untrusted workspace ───────────────────────────────────────────────────────────


def test_the_manager_starts_untrusted(workspace, home):
    assert PluginManager(workspace=workspace).workspace_trusted is False


def test_an_untrusted_workspace_plugin_is_not_loaded_or_exposed(workspace, home):
    make_plugin(workspace_plugins(workspace), "evil")
    manager = PluginManager(workspace=workspace)

    assert manager.load() == []
    assert manager.get_plugin("evil") is None
    assert manager.plugin_count == 0
    assert manager.all_commands() == []
    assert manager.find_command("review") is None
    assert manager.all_skills() == []


def test_an_untrusted_workspace_plugin_wires_no_hooks_and_no_mcp_servers(workspace, home):
    make_plugin(workspace_plugins(workspace), "evil")
    manager = PluginManager(workspace=workspace)
    manager.load()
    dispatcher = HookDispatcher(workspace=workspace, trusted=False)
    registry = MCPServerRegistry(workspace=workspace)

    assert manager.wire_hooks(dispatcher) == 0
    assert manager.wire_mcp(registry) == 0
    assert hook_commands(dispatcher) == []
    assert registry._entries == {}


def test_an_untrusted_workspace_cannot_shadow_a_user_plugin(workspace, home):
    make_plugin(user_plugins(home), "shared", hooks=False, mcp=False)
    make_plugin(workspace_plugins(workspace), "shared", hooks=False, mcp=False)
    manager = PluginManager(workspace=workspace)
    manager.load()

    plugin = manager.get_plugin("shared")
    assert plugin is not None and plugin.origin == ORIGIN_USER
    assert manager.find_command("review").body == "BODY OF shared"
    assert manager.plugin_count == 1


def test_user_plugins_load_whatever_the_workspace_trust(workspace, home):
    make_plugin(user_plugins(home), "mine")
    for trusted in (False, True):
        manager = PluginManager(workspace=workspace, workspace_trusted=trusted)
        manager.load()
        assert [p.name for p in manager._active_plugins()] == ["mine"]
        assert manager.wire_hooks(HookDispatcher(workspace=workspace)) == 1
        assert manager.wire_mcp(MCPServerRegistry(workspace=workspace)) == 1


def test_a_manager_without_a_workspace_still_loads_user_plugins(home):
    make_plugin(user_plugins(home), "mine")
    manager = PluginManager(workspace=None)
    assert [p.name for p in manager.load()] == ["mine"]


def test_caller_supplied_search_paths_still_load(workspace, home, tmp_path):
    extra = tmp_path / "extra"
    make_plugin(extra, "added")
    manager = PluginManager(workspace=workspace, extra_search_paths=[extra])
    assert [p.name for p in manager.load()] == ["added"]


# ── manager: a trusted workspace ──────────────────────────────────────────────────────────────


def test_a_trusted_workspace_plugin_loads_and_is_wired(workspace, home):
    make_plugin(workspace_plugins(workspace), "good")
    manager = PluginManager(workspace=workspace)
    manager.set_workspace_trusted(True)
    dispatcher = HookDispatcher(workspace=workspace, trusted=True)
    registry = MCPServerRegistry(workspace=workspace)

    assert [p.name for p in manager.load()] == ["good"]
    assert manager.find_command("review").body == "BODY OF good"
    assert manager.wire_hooks(dispatcher) == 1
    assert manager.wire_mcp(registry) == 1
    assert hook_commands(dispatcher) == ["echo HOOK"]
    assert list(registry._entries) == ["good:srv"]


def test_the_constructor_can_declare_the_workspace_trusted(workspace, home):
    make_plugin(workspace_plugins(workspace), "good")
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    assert [p.name for p in manager.load()] == ["good"]


# ── manager: defence in depth at the registration boundaries ──────────────────────────────────


def _loaded_trusted_then_revoked(workspace: Path) -> PluginManager:
    make_plugin(workspace_plugins(workspace), "evil")
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    manager.load()
    assert manager.get_plugin("evil") is not None
    manager.set_workspace_trusted(False)
    return manager


def test_wire_hooks_refuses_a_workspace_plugin_that_reached_it_by_another_path(
    workspace, home, caplog
):
    manager = _loaded_trusted_then_revoked(workspace)
    dispatcher = HookDispatcher(workspace=workspace, trusted=False)

    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        assert manager.wire_hooks(dispatcher) == 0

    assert hook_commands(dispatcher) == []
    assert "Refusing to wire hooks from workspace plugin 'evil'" in caplog.text


def test_wire_mcp_refuses_a_workspace_plugin_that_reached_it_by_another_path(
    workspace, home, caplog
):
    manager = _loaded_trusted_then_revoked(workspace)
    registry = MCPServerRegistry(workspace=workspace)

    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        assert manager.wire_mcp(registry) == 0

    assert registry._entries == {}
    assert "Refusing to register MCP servers from workspace plugin 'evil'" in caplog.text


def test_the_commands_of_a_workspace_plugin_are_withheld_when_trust_is_not_held(workspace, home):
    manager = _loaded_trusted_then_revoked(workspace)
    assert manager.all_commands() == []
    assert manager.find_command("review") is None


def test_a_plugin_injected_without_an_origin_is_refused(workspace, home, caplog):
    # A hooks file that exists and parses: the refusal has to be the trust check, not a miss.
    root = make_plugin(workspace_plugins(workspace), "injected")
    manifest = DeclarativePluginManifest.load(root)
    manager = PluginManager(workspace=workspace)  # untrusted
    manager._plugins["injected"] = DiscoveredPlugin(manifest=manifest, has_hooks=True, has_mcp=True)
    dispatcher = HookDispatcher(workspace=workspace)
    registry = MCPServerRegistry(workspace=workspace)

    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        assert manager.wire_hooks(dispatcher) == 0
        assert manager.wire_mcp(registry) == 0

    assert hook_commands(dispatcher) == [] and registry._entries == {}
    assert caplog.text.count("Refusing") == 2


def test_a_user_plugin_is_not_refused_by_the_boundary_checks(workspace, home, caplog):
    make_plugin(user_plugins(home), "mine")
    manager = PluginManager(workspace=workspace)  # untrusted workspace
    manager.load()

    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        assert manager.wire_hooks(HookDispatcher(workspace=workspace)) == 1
        assert manager.wire_mcp(MCPServerRegistry(workspace=workspace)) == 1
    assert "Refusing" not in caplog.text


# ── load_and_register_plugins: the decision is passed in ──────────────────────────────────────


def _session(workspace: Path, *, trusted_dispatcher: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        console=MagicMock(),
        _registry=SlashCommandRegistry(),
        _completer=None,
        _command_palette=None,
        _plugin_manager=PluginManager(workspace=workspace),
        _hook_dispatcher=HookDispatcher(workspace=workspace, trusted=trusted_dispatcher),
        _mcp_registry=MCPServerRegistry(workspace=workspace),
    )


async def test_load_and_register_plugins_defaults_to_untrusted(workspace, home):
    make_plugin(workspace_plugins(workspace), "evil")
    repl = _session(workspace)

    await load_and_register_plugins(repl)  # no decision given: fail closed

    assert repl._registry.get("review") is None
    assert hook_commands(repl._hook_dispatcher) == []
    assert repl._mcp_registry._entries == {}


async def test_load_and_register_plugins_registers_nothing_for_an_untrusted_workspace(
    workspace, home
):
    make_plugin(workspace_plugins(workspace), "evil")
    make_plugin(user_plugins(home), "mine", command="mine-cmd")
    repl = _session(workspace)

    await load_and_register_plugins(repl, workspace_trusted=False)

    assert repl._registry.get("review") is None
    assert repl._registry.get("mine-cmd") is not None  # user-level plugin unaffected
    assert hook_commands(repl._hook_dispatcher) == ["echo HOOK"]  # only the user's plugin
    assert list(repl._mcp_registry._entries) == ["mine:srv"]


async def test_load_and_register_plugins_registers_a_trusted_workspace(workspace, home):
    make_plugin(workspace_plugins(workspace), "good")
    repl = _session(workspace, trusted_dispatcher=True)

    await load_and_register_plugins(repl, workspace_trusted=True)

    assert repl._registry.get("review") is not None
    assert hook_commands(repl._hook_dispatcher) == ["echo HOOK"]
    assert list(repl._mcp_registry._entries) == ["good:srv"]


async def test_the_decision_reaches_the_manager_before_it_loads():
    repl = SimpleNamespace(_plugin_manager=MagicMock(), console=MagicMock())
    repl._plugin_manager.load.return_value = []
    calls: list[str] = []
    repl._plugin_manager.set_workspace_trusted.side_effect = lambda v: calls.append(f"trust={v}")
    repl._plugin_manager.load.side_effect = lambda: calls.append("load") or []

    await load_and_register_plugins(repl, workspace_trusted=True)

    assert calls == ["trust=True", "load"]


# ── the trust prompt ──────────────────────────────────────────────────────────────────────────


def _ask(workspace: Path, monkeypatch, *, stored: bool = False, answer: bool = False):
    from rich import prompt

    asked: list[str] = []
    trusted: list[Path] = []
    monkeypatch.setattr(trust, "is_trusted", lambda path: stored)
    monkeypatch.setattr(trust, "trust", lambda path: trusted.append(path))

    def ask(*args, **kwargs):
        asked.append("asked")
        return answer

    monkeypatch.setattr(prompt.Confirm, "ask", ask)
    stub = SimpleNamespace(_mcp_registry=SimpleNamespace(workspace=workspace), console=MagicMock())
    return VeluneREPL._ensure_workspace_trust(stub), asked, trusted


def test_a_workspace_with_only_a_plugin_prompts_for_trust(workspace, monkeypatch):
    make_plugin(workspace_plugins(workspace), "evil")
    decision, asked, trusted = _ask(workspace, monkeypatch, answer=False)
    assert decision is False
    assert asked == ["asked"]  # it used to be silently treated as "nothing to trust"
    assert trusted == []


def test_accepting_the_prompt_trusts_the_workspace(workspace, monkeypatch):
    make_plugin(workspace_plugins(workspace), "evil")
    decision, asked, trusted = _ask(workspace, monkeypatch, answer=True)
    assert decision is True
    assert asked == ["asked"] and trusted == [workspace]


def test_a_workspace_without_plugins_or_other_config_does_not_prompt(workspace, monkeypatch):
    workspace_plugins(workspace).mkdir(parents=True)  # an empty plugins folder is not config
    decision, asked, _ = _ask(workspace, monkeypatch)
    assert decision is False and asked == []


def test_an_already_trusted_workspace_does_not_prompt_again(workspace, monkeypatch):
    make_plugin(workspace_plugins(workspace), "evil")
    decision, asked, _ = _ask(workspace, monkeypatch, stored=True)
    assert decision is True and asked == []


def test_trust_all_still_bypasses_the_prompt(workspace, trust_file, monkeypatch):
    make_plugin(workspace_plugins(workspace), "evil")
    monkeypatch.setenv("VELUNE_TRUST_ALL", "1")
    stub = SimpleNamespace(_mcp_registry=SimpleNamespace(workspace=workspace), console=MagicMock())
    assert VeluneREPL._ensure_workspace_trust(stub) is True


def test_a_workspace_trusted_with_velune_trust_add_is_recognised(workspace, trust_file):
    trust.trust(workspace)
    assert trust.is_trusted(workspace)
    stub = SimpleNamespace(_mcp_registry=SimpleNamespace(workspace=workspace), console=MagicMock())
    assert VeluneREPL._ensure_workspace_trust(stub) is True


# ── the real VeluneREPL.run() start-up order ──────────────────────────────────────────────────


class _EndOfInput:
    """A prompt session whose first read ends the session, so run() returns."""

    async def prompt_async(self, **kwargs):
        raise EOFError


def _repl_for_run(workspace: Path, events: list[str], monkeypatch) -> MagicMock:
    """A ``VeluneREPL`` stand-in running the *real* ``run()``, ``_ensure_workspace_trust``,
    ``load_and_register_plugins``, plugin manager, hook dispatcher and MCP registry.

    Only the edges that would touch the outside world are replaced: the MCP connection, the
    hook subprocess runner, and the UI/session machinery.
    """
    from velune.cli.commands import file_commands
    from velune.hooks import dispatcher as hook_dispatcher_module
    from velune.hooks.types import HookResult

    monkeypatch.setattr(file_commands, "_USER_COMMANDS_DIR", workspace.parent / "no-commands")
    monkeypatch.setattr("velune.cli.handlers.model.restore_active_model", lambda repl: None)
    monkeypatch.setattr(
        "velune.cli.handlers.cognition.auto_detect_on_entry", AsyncMock(return_value=None)
    )

    async def no_hook_runs(defs, payload, workspace=None):
        events.append("hook-subprocess")
        return HookResult()

    monkeypatch.setattr(hook_dispatcher_module, "run_hooks_parallel", no_hook_runs)

    async def connect(self, name):
        events.append(f"connect:{name}")
        return False

    monkeypatch.setattr(MCPServerRegistry, "connect", connect)

    real_load = PluginManager.load

    def load(self):
        events.append("plugins-load")
        return real_load(self)

    monkeypatch.setattr(PluginManager, "load", load)

    repl = MagicMock()
    repl.console = MagicMock()
    repl.active_model = None
    repl._episodic_session_id = None
    repl._exit_requested = False
    repl._pending_prompt_text = ""
    repl._start_episodic_session = AsyncMock()
    repl._shutdown_repl = AsyncMock()
    repl._reverify_stale_keys = AsyncMock()
    repl._ollama_probe_loop = AsyncMock()
    repl._build_plain_session = lambda: _EndOfInput()
    repl.container = SimpleNamespace(get=lambda key: str(workspace), get_optional=lambda key: None)
    repl._registry = SlashCommandRegistry()
    repl._hook_dispatcher = HookDispatcher(workspace=workspace, session_id=None)
    repl._mcp_registry = MCPServerRegistry(workspace=workspace)
    repl._mcp_watch_task = None
    repl._plugin_manager = PluginManager(workspace=workspace)
    repl._ensure_workspace_trust = lambda: VeluneREPL._ensure_workspace_trust(repl)
    return repl


def _answer_prompt(monkeypatch, answer: bool, events: list[str]) -> None:
    from rich import prompt

    def ask(*args, **kwargs):
        events.append("trust-prompt")
        return answer

    monkeypatch.setattr(prompt.Confirm, "ask", ask)


async def _run(repl: MagicMock) -> None:
    try:
        await VeluneREPL.run(repl, plain=True)
    finally:
        task = repl._mcp_watch_task
        if task is not None:
            task.cancel()


def _plugin_hook_commands(repl) -> list[str]:
    return hook_commands(repl._hook_dispatcher)


async def test_declining_trust_keeps_a_workspace_plugin_completely_inactive(
    workspace, home, trust_file, monkeypatch
):
    make_plugin(workspace_plugins(workspace), "evil")
    events: list[str] = []
    _answer_prompt(monkeypatch, False, events)
    repl = _repl_for_run(workspace, events, monkeypatch)

    await _run(repl)

    assert events[0] == "trust-prompt"
    assert repl._plugin_manager.plugin_count == 0
    assert repl._registry.get("review") is None
    assert _plugin_hook_commands(repl) == []
    assert "evil:srv" not in repl._mcp_registry._entries
    assert not [e for e in events if e.startswith("connect:")]  # nothing was spawned
    assert "hook-subprocess" not in events
    assert not trust.is_trusted(workspace)


async def test_accepting_trust_loads_the_plugin_after_the_decision_and_before_connecting(
    workspace, home, trust_file, monkeypatch
):
    make_plugin(workspace_plugins(workspace), "good")
    events: list[str] = []
    _answer_prompt(monkeypatch, True, events)
    repl = _repl_for_run(workspace, events, monkeypatch)

    await _run(repl)

    assert events.index("trust-prompt") < events.index("plugins-load")
    assert events.index("plugins-load") < events.index("connect:good:srv")
    assert repl._registry.get("review") is not None
    assert _plugin_hook_commands(repl) == ["echo HOOK"]
    assert trust.is_trusted(workspace)


async def test_a_previously_trusted_workspace_loads_its_plugin_without_a_prompt(
    workspace, home, trust_file, monkeypatch
):
    make_plugin(workspace_plugins(workspace), "good")
    trust.trust(workspace)
    events: list[str] = []
    _answer_prompt(monkeypatch, False, events)  # would decline, if it were asked
    repl = _repl_for_run(workspace, events, monkeypatch)

    await _run(repl)

    assert "trust-prompt" not in events
    assert "connect:good:srv" in events


async def test_user_plugins_still_start_in_an_untrusted_workspace(
    workspace, home, trust_file, monkeypatch
):
    make_plugin(workspace_plugins(workspace), "evil")
    make_plugin(user_plugins(home), "mine", command="mine-cmd")
    events: list[str] = []
    _answer_prompt(monkeypatch, False, events)
    repl = _repl_for_run(workspace, events, monkeypatch)

    await _run(repl)

    assert repl._registry.get("mine-cmd") is not None
    assert repl._registry.get("review") is None
    assert "connect:mine:srv" in events
    assert "connect:evil:srv" not in events


async def test_trust_is_resolved_before_any_plugin_is_loaded(
    workspace, home, trust_file, monkeypatch
):
    """The order is the guarantee: nothing from the workspace is registered ahead of it."""
    make_plugin(workspace_plugins(workspace), "evil")
    events: list[str] = []
    _answer_prompt(monkeypatch, False, events)
    repl = _repl_for_run(workspace, events, monkeypatch)

    await _run(repl)

    assert events.index("trust-prompt") < events.index("plugins-load")
