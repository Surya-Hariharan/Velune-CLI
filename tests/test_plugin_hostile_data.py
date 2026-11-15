"""Malformed plugin data is contained to the plugin that holds it.

Everything a plugin ships is data written by its author, of any shape: frontmatter, manifests,
hook files, MCP files. A repository can ship a plugin, so one hostile plugin in a trusted
workspace must never stop the user's own plugins from loading (their hooks included). Each case
below puts one malformed file in a plugin next to a healthy user plugin and runs the real
start-up; the user's hook, command and MCP server must all still be there and nothing may raise.
The matrix runs under both YAML modes: PyYAML (typed values, strict) and the stdlib fallback
used when PyYAML is not installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from velune.cli.handlers.plugins import load_and_register_plugins
from velune.cli.slash_dispatcher import build_slash_registry
from velune.core.child_env import plugin_child_env
from velune.hooks.dispatcher import HookDispatcher
from velune.mcp.registry import MCPServerRegistry, ServerState
from velune.mcp.transports.base import ServerConfig
from velune.plugins.declarative import command as command_module
from velune.plugins.declarative.command import _parse_simple_yaml, parse_command_file
from velune.plugins.declarative.skill import parse_skill_file
from velune.plugins.manager import PluginManager, _validate_mcp_entry

J = json.dumps
GOOD_HOOK = {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo MINE"}]}]}


def require_pyyaml() -> None:
    """Skip when PyYAML is not installed (the lowest-dependency CI jobs run without it)."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("PyYAML is not installed")


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


@pytest.fixture(params=["pyyaml", "stdlib-fallback"])
def yaml_mode(request, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run under PyYAML, and under the fallback parser used when PyYAML is not installed."""
    if request.param == "pyyaml":
        require_pyyaml()
    else:
        monkeypatch.setitem(sys.modules, "yaml", None)  # makes `import yaml` raise ImportError
    return request.param


def write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def user_plugin(home: Path) -> None:
    base = home / ".velune" / "plugins" / "mine"
    write(base / ".velune-plugin" / "plugin.json", J({"name": "mine"}))
    write(base / "hooks" / "hooks.json", J(GOOD_HOOK))
    write(base / ".mcp.json", J({"srv": {"command": "echo"}}))
    write(base / "commands" / "mine-cmd.md", "mine body")


def hostile_plugin(
    workspace: Path, *, manifest=None, command=None, skill=None, hooks=None, mcp=None
) -> None:
    base = workspace / ".velune" / "plugins" / "hostile"
    write(
        base / ".velune-plugin" / "plugin.json",
        manifest if manifest is not None else J({"name": "hostile"}),
    )
    if command is not None:
        write(base / "commands" / "evil.md", command)
    if skill is not None:
        write(base / "skills" / "evil" / "SKILL.md", skill)
    if hooks is not None:
        write(base / "hooks" / "hooks.json", hooks)
    if mcp is not None:
        write(base / ".mcp.json", mcp)


async def startup(workspace: Path) -> tuple[dict, MagicMock]:
    repl = MagicMock()
    repl.container = SimpleNamespace(get=lambda k: str(workspace), get_optional=lambda k: None)
    repl._completer = repl._command_palette = None
    repl._registry = build_slash_registry(repl)
    repl._plugin_manager = PluginManager(workspace=workspace)
    repl._hook_dispatcher = HookDispatcher(workspace=workspace, trusted=True)
    repl._mcp_registry = MCPServerRegistry(workspace=workspace)
    await load_and_register_plugins(repl, workspace_trusted=True)
    hooks = [h.command for b in repl._hook_dispatcher._ensure_loaded() for h in b.hooks]
    state = {
        "hook": "echo MINE" in hooks,
        "command": repl._registry.get("mine-cmd") is not None,
        "mcp": "mine:srv" in repl._mcp_registry._entries,
    }
    return state, repl


def printed(repl: MagicMock) -> str:
    return " ".join(str(call.args[0]) for call in repl.console.print.call_args_list if call.args)


# (id, hostile_plugin keyword arguments)
VARIANTS = [
    # command frontmatter
    ("cmd-fm-list", {"command": "---\n- bad\n---\nbody"}),
    ("cmd-fm-scalar", {"command": "---\njust a scalar\n---\nbody"}),
    ("cmd-fm-syntax-error", {"command": "---\naliases: [unclosed\n---\nbody"}),
    ("cmd-fm-python-tag", {"command": "---\nx: !!python/object:os.system 'echo'\n---\nbody"}),
    ("cmd-fm-nul-byte", {"command": b"---\nx: a\x00b\n---\nbody"}),
    ("cmd-fm-description-mapping", {"command": "---\ndescription: {a: b}\n---\nbody"}),
    ("cmd-fm-aliases-numbers", {"command": "---\naliases: [1, null]\n---\nbody"}),
    ("cmd-fm-tab-indent", {"command": "---\naliases:\n\t- x\n---\nbody"}),
    ("cmd-not-utf8", {"command": b"\xff\xfe\x00 not utf-8 \xff"}),
    # skill frontmatter
    ("skill-fm-list", {"skill": "---\n- bad\n---\nbody"}),
    ("skill-fm-syntax-error", {"skill": "---\ntriggers: [unclosed\n---\nbody"}),
    ("skill-fm-numbers", {"skill": "---\ntriggers: [1, 2]\nname: 5\n---\nbody"}),
    ("skill-fm-always-list", {"skill": "---\nalways: [x]\n---\nbody"}),
    # plugin.json
    ("manifest-list", {"manifest": "[]"}),
    ("manifest-string", {"manifest": '"x"'}),
    ("manifest-not-json", {"manifest": "{nope"}),
    ("manifest-name-mapping", {"manifest": J({"name": {"a": 1}})}),
    ("manifest-keywords-int", {"manifest": J({"name": "hostile", "keywords": 5})}),
    ("manifest-author-int", {"manifest": J({"name": "hostile", "author": 5})}),
    ("manifest-path-list", {"manifest": J({"name": "hostile", "hooks": ["a"]})}),
    ("manifest-version-mapping", {"manifest": J({"name": "hostile", "version": {"a": 1}})}),
    # hooks.json
    ("hooks-root-list", {"hooks": "[]"}),
    ("hooks-hooks-list", {"hooks": J({"hooks": []})}),
    ("hooks-event-string", {"hooks": J({"PreToolUse": "x"})}),
    ("hooks-hooks-string", {"hooks": J({"PreToolUse": [{"hooks": "x"}]})}),
    (
        "hooks-command-null",
        {"hooks": J({"PreToolUse": [{"hooks": [{"type": "command", "command": None}]}]})},
    ),
    (
        "hooks-command-int",
        {"hooks": J({"PreToolUse": [{"hooks": [{"type": "command", "command": 5}]}]})},
    ),
    (
        "hooks-timeout-string",
        {
            "hooks": J(
                {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "timeout": "n"}]}]}
            )
        },
    ),
    (
        "hooks-matcher-int",
        {
            "hooks": J(
                {"PreToolUse": [{"matcher": 5, "hooks": [{"type": "command", "command": "x"}]}]}
            )
        },
    ),
    (
        "hooks-env-int",
        {"hooks": J({"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "env": 5}]}]})},
    ),
    (
        "hooks-condition-int",
        {"hooks": J({"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "if": 5}]}]})},
    ),
    ("hooks-not-json", {"hooks": "{nope"}),
    # .mcp.json
    ("mcp-root-list", {"mcp": "[]"}),
    ("mcp-server-int", {"mcp": J({"s": 5})}),
    ("mcp-command-null", {"mcp": J({"s": {"command": None}})}),
    ("mcp-command-int", {"mcp": J({"s": {"command": 5}})}),
    ("mcp-args-string", {"mcp": J({"s": {"command": "x", "args": "abc"}})}),
    ("mcp-args-non-text", {"mcp": J({"s": {"command": "x", "args": [1, None]}})}),
    ("mcp-env-int", {"mcp": J({"s": {"command": "x", "env": 5}})}),
    ("mcp-env-values-non-text", {"mcp": J({"s": {"command": "x", "env": {"A": 5, "B": None}}})}),
    ("mcp-headers-list", {"mcp": J({"s": {"url": "http://h", "type": "sse", "headers": [1]}})}),
    ("mcp-url-int", {"mcp": J({"s": {"type": "sse", "url": 5}})}),
    ("mcp-timeout-string", {"mcp": J({"s": {"command": "x", "timeout": "no"}})}),
    ("mcp-timeout-null", {"mcp": J({"s": {"command": "x", "timeout": None}})}),
    ("mcp-empty-name", {"mcp": J({"": {"command": "x"}})}),
    ("mcp-type-list", {"mcp": J({"s": {"command": "x", "type": ["sse"]}})}),
    ("mcp-not-json", {"mcp": "{nope"}),
]


@pytest.mark.parametrize(("variant", "kwargs"), VARIANTS, ids=[v[0] for v in VARIANTS])
async def test_a_hostile_plugin_cannot_degrade_a_healthy_one(
    workspace, home, yaml_mode, variant, kwargs, caplog
):
    user_plugin(home)
    hostile_plugin(workspace, **kwargs)

    with caplog.at_level(logging.WARNING):
        state, repl = await startup(workspace)  # must not raise

    assert state == {"hook": True, "command": True, "mcp": True}, state
    assert "Plugin load failed" not in printed(repl)


# ── the parsers: a handled failure, not another exception type ────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "- a\n- b",  # a list
        "just a scalar",
        "aliases: [unclosed",  # syntax error
        "x: !!python/object:os.system 'echo'",  # unsafe tag
        "x: a\x00b",  # NUL
        "a:\n\t- b",  # tab indentation
    ],
)
def test_malformed_yaml_is_a_valueerror_and_nothing_else(text):
    require_pyyaml()
    with pytest.raises(ValueError):
        _parse_simple_yaml(text)


@pytest.mark.parametrize("text", ["", "# only a comment", "a: 1\nb: [x, y]"])
def test_valid_or_empty_yaml_still_parses(text):
    assert isinstance(_parse_simple_yaml(text), dict)


def test_a_command_with_invalid_frontmatter_is_skipped_with_a_warning(tmp_path, caplog):
    path = tmp_path / "cmd.md"
    path.write_text("---\n- not a mapping\n---\nbody", encoding="utf-8")
    require_pyyaml()
    with caplog.at_level(logging.WARNING, logger="velune.plugins.declarative.command"):
        assert parse_command_file(path, "p") is None
    assert "invalid frontmatter" in caplog.text


def test_the_skip_does_not_depend_on_which_yaml_library_is_installed(tmp_path, monkeypatch, caplog):
    """A parser that rejects the frontmatter, whatever it is, skips that file."""

    def reject(text):
        raise ValueError("frontmatter must be a mapping, not list")

    monkeypatch.setattr(command_module, "_parse_simple_yaml", reject)
    command_file = tmp_path / "cmd.md"
    command_file.write_text("---\nx: y\n---\nbody", encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nx: y\n---\nbody", encoding="utf-8")
    monkeypatch.setattr("velune.plugins.declarative.skill._parse_simple_yaml", reject)

    with caplog.at_level(logging.WARNING):
        assert parse_command_file(command_file, "p") is None
        assert parse_skill_file(skill_dir, "p") is None
    assert caplog.text.count("invalid frontmatter") == 2


# ── MCP entries: field types are validated before use ─────────────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        {"command": None},
        {"command": 5},
        {"command": "x", "args": "abc"},
        {"command": "x", "args": [1]},
        {"command": "x", "args": None},
        {"url": 5},
        {"url": "u", "type": ["sse"]},
        {"command": "x", "env": 5},
        {"url": "u", "headers": [1]},
        {"url": "u", "headers": {"A": 5}},
    ],
)
def test_an_mcp_entry_with_the_wrong_field_types_is_rejected(entry):
    with pytest.raises(ValueError, match="server 's'"):
        _validate_mcp_entry("s", entry)


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "node", "args": ["a", "b"], "env": {"K": "v"}},
        {"url": "http://h/sse", "type": "sse", "headers": {"A": "b"}},
        {"command": "x"},
        {},
    ],
)
def test_a_well_formed_mcp_entry_is_accepted(entry):
    _validate_mcp_entry("s", entry)  # does not raise


@pytest.mark.parametrize(
    "servers",
    [
        {"s": {"command": "x", "args": "abc"}},  # used to be read as ["a", "b", "c"]
        {"good": {"command": "x"}, "bad": {"command": None}},
        {"good": {"command": "x"}, "bad": {"command": "x", "args": [1]}},
    ],
)
def test_a_plugin_with_an_invalid_server_registers_none_of_its_servers(
    workspace, home, servers, caplog
):
    hostile_plugin(workspace, mcp=J(servers))
    manager = PluginManager(workspace=workspace, workspace_trusted=True)
    manager.load()
    registry = MCPServerRegistry(workspace=workspace)

    with caplog.at_level(logging.WARNING, logger="velune.plugins.manager"):
        assert manager.wire_mcp(registry) == 0

    assert registry._entries == {}
    assert "Failed to wire MCP from plugin 'hostile'" in caplog.text


# ── one failing step does not stop the others ─────────────────────────────────────────────────


async def test_a_failing_setup_step_does_not_stop_the_others(workspace, home, monkeypatch):
    user_plugin(home)
    import velune.cli.handlers.plugins as handlers

    def boom(repl, plugins):
        raise RuntimeError("simulated command-registration failure")

    monkeypatch.setattr(handlers, "register_plugin_commands", boom)

    state, repl = await startup(workspace)

    assert state["hook"] is True and state["mcp"] is True  # wired despite the failure
    assert state["command"] is False
    assert "Some plugin setup failed" in printed(repl)
    assert "commands (RuntimeError)" in printed(repl)


@pytest.mark.parametrize(
    "error", [KeyError("k"), AttributeError("a"), RuntimeError("r"), RecursionError()]
)
def test_whatever_a_plugin_raises_while_loading_only_skips_that_plugin(
    workspace, home, monkeypatch, error
):
    """The backstop: not just the exception types anyone thought of."""
    from velune.plugins.declarative import scanner as scanner_module

    user_plugin(home)
    hostile_plugin(workspace, command="c")
    real = scanner_module.load_plugin_commands

    def flaky(commands_dir, plugin_name, plugin_root=None):
        if plugin_name == "hostile":
            raise error
        return real(commands_dir, plugin_name, plugin_root=plugin_root)

    monkeypatch.setattr(scanner_module, "load_plugin_commands", flaky)
    manager = PluginManager(workspace=workspace, workspace_trusted=True)

    assert [p.name for p in manager.load()] == ["mine"]


# ── an MCP connection that finishes after its server was detached ─────────────────────────────


class SlowConnection:
    """A connection whose handshake waits to be released, recording how it was torn down."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    async def connect(self) -> None:
        await self.release.wait()

    async def disconnect(self) -> None:
        self.closed = True

    async def list_tools(self):
        return []

    async def list_resources(self):
        return []


async def test_a_connection_that_completes_after_detach_is_closed_not_attached(monkeypatch):
    import velune.mcp.registry as registry_module

    connection = SlowConnection()
    monkeypatch.setattr(registry_module, "make_connection", lambda config, **kw: connection)
    registry = MCPServerRegistry()
    registry.register(ServerConfig.from_dict("p:s", {"command": "echo"}), owner="p")

    attempt = asyncio.create_task(registry.connect("p:s"))
    await asyncio.sleep(0)  # the handshake is now waiting
    detached = registry.detach_owner("p")  # the plugin is disabled mid-handshake
    connection.release.set()

    assert await attempt is False
    assert connection.closed is True  # the process was closed, not left running
    assert detached[0].connection is None  # and never attached to the orphaned entry
    assert detached[0].state != ServerState.CONNECTED
    assert registry._entries == {}


async def test_a_connection_that_completes_normally_is_still_attached(monkeypatch):
    import velune.mcp.registry as registry_module

    connection = SlowConnection()
    monkeypatch.setattr(registry_module, "make_connection", lambda config, **kw: connection)
    registry = MCPServerRegistry()
    registry.register(ServerConfig.from_dict("p:s", {"command": "echo"}), owner="p")

    attempt = asyncio.create_task(registry.connect("p:s"))
    await asyncio.sleep(0)
    connection.release.set()

    assert await attempt is True
    assert registry._entries["p:s"].is_connected and connection.closed is False


# ── flush waits for cleanup that is already running ───────────────────────────────────────────


class SlowToClose:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def disconnect(self) -> None:
        self.started.set()
        await self.release.wait()
        self.closed = True


async def test_a_second_flush_waits_for_a_close_that_is_still_running(workspace, home):
    user_plugin(home)
    manager = PluginManager(workspace=workspace)
    registry = MCPServerRegistry(workspace=workspace)
    manager.load()
    manager.wire_mcp(registry)
    slow = SlowToClose()
    entry = registry._entries["mine:srv"]
    entry.connection = slow  # type: ignore[assignment]
    entry.state = ServerState.CONNECTED

    manager.disable("mine")  # schedules the background flush
    first = asyncio.create_task(manager.flush())
    await slow.started.wait()  # a close is now in progress
    second = asyncio.create_task(manager.flush())
    await asyncio.sleep(0.05)

    assert not second.done(), "the second flush returned while a close was still running"
    slow.release.set()
    await asyncio.gather(first, second)
    assert slow.closed is True


# ── only the standard locale variables are forwarded ──────────────────────────────────────────

STANDARD_LOCALE = [
    "LC_ADDRESS",
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_IDENTIFICATION",
    "LC_MEASUREMENT",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NAME",
    "LC_NUMERIC",
    "LC_PAPER",
    "LC_TELEPHONE",
    "LC_TIME",
]


@pytest.mark.parametrize("windows", [False, True])
def test_an_lc_named_secret_is_not_forwarded_but_the_standard_locale_is(windows):
    base = dict.fromkeys(STANDARD_LOCALE, "C")
    base.update({"LC_PRIVATE_TOKEN": "lc-SECRET", "LC_": "x", "LC_CUSTOM": "y", "PATH": "/bin"})

    env = plugin_child_env(base=base, windows=windows)

    assert not {"LC_PRIVATE_TOKEN", "LC_", "LC_CUSTOM"} & set(env)
    assert all(name in env for name in STANDARD_LOCALE)
    assert not any("SECRET" in value for value in env.values())
