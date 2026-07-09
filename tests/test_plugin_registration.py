"""Regression tests for plugin slash-command registration.

Covers two bugs found during the slash-command audit:

1. ``register_plugin_commands`` imported ``SlashCommand`` from a module that
   never existed (``velune.cli.slash_registry`` instead of
   ``velune.cli.slash_commands``), so *any* plugin with commands raised
   ``ModuleNotFoundError`` the instant it tried to register.
2. That failure (and any other plugin-load failure) was swallowed at DEBUG
   level inside ``VeluneREPL.run()``, and the app's log threshold is WARNING,
   so it was completely invisible to the user — plugins silently never loaded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from velune.cli.handlers.plugins import register_plugin_commands
from velune.cli.slash_commands import SlashCommandRegistry


@dataclass
class _FakeCommand:
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = "does a thing"
    usage: str = "/thing"

    @property
    def help_label(self) -> str:
        return "(plugin)"

    def render(self, args: str, plugin_root) -> str:
        return f"rendered {self.name} {args}"


@dataclass
class _FakePlugin:
    root: str = "/fake/plugin"
    commands: list = field(default_factory=list)


def _make_repl():
    repl = MagicMock()
    repl._registry = SlashCommandRegistry()
    repl._completer = None
    return repl


def test_register_plugin_commands_imports_the_real_module():
    """The historical bug: this used to raise ModuleNotFoundError on import."""
    repl = _make_repl()
    plugin = _FakePlugin(commands=[_FakeCommand(name="review", aliases=["rv"])])

    register_plugin_commands(repl, [plugin])  # must not raise

    cmd = repl._registry.get("review")
    assert cmd is not None
    assert cmd.name == "review"
    assert repl._registry.get("rv") is cmd  # alias resolves to the same command


async def test_registered_plugin_command_handler_invokes_prompt():
    """The generated handler should render the plugin template and forward it."""
    repl = _make_repl()
    repl._handle_prompt = AsyncMock()
    plugin = _FakePlugin(commands=[_FakeCommand(name="review")])
    register_plugin_commands(repl, [plugin])

    cmd = repl._registry.get("review")
    await cmd.handler("some args")

    repl._handle_prompt.assert_awaited_once_with("rendered review some args")


async def test_plugin_load_failure_is_logged_at_warning_not_debug(caplog):
    """A plugin-load exception must be visible at the app's default log level
    (WARNING), not swallowed at DEBUG where nobody will ever see it."""
    from velune.cli.handlers.plugins import load_and_register_plugins

    repl = MagicMock()
    repl._plugin_manager.load.side_effect = ModuleNotFoundError(
        "No module named 'velune.cli.slash_registry'"
    )
    repl.console = MagicMock()

    caplog.set_level(logging.WARNING, logger="velune.cli.handlers.plugins")

    await load_and_register_plugins(repl)  # must not raise

    assert any("Plugin load error" in r.message for r in caplog.records)
    repl.console.print.assert_called_once()
    assert "Plugin load failed" in repl.console.print.call_args[0][0]


async def test_load_and_register_plugins_wires_hooks_and_mcp_on_success():
    from velune.cli.handlers.plugins import load_and_register_plugins

    repl = _make_repl()
    repl.console = MagicMock()
    plugin = _FakePlugin(commands=[_FakeCommand(name="review")])
    repl._plugin_manager.load.return_value = [plugin]

    await load_and_register_plugins(repl)

    assert repl._registry.get("review") is not None
    repl._plugin_manager.wire_hooks.assert_called_once_with(repl._hook_dispatcher)
    repl._plugin_manager.wire_mcp.assert_called_once_with(repl._mcp_registry)
    repl.console.print.assert_not_called()  # success path is silent
