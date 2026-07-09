"""Tests for the SlashCommand.permissions confirm-gate and the /memory clear fix.

Phase-3-style requirement: commands carry declarative permission metadata,
and a destructive action never runs without an actionable confirmation.
Before this fix, /memory clear wiped working memory immediately with no
confirmation at all — the one genuinely unguarded destructive built-in found
during the audit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from velune.cli.handlers.confirm import confirm_destructive
from velune.cli.handlers.memory import cmd_memory
from velune.cli.slash_commands import SlashCommand, SlashCommandRegistry


def test_slash_command_permissions_defaults_to_empty():
    cmd = SlashCommand(name="x", aliases=[], description="d", usage="/x", handler=AsyncMock())
    assert cmd.permissions == ()


async def test_confirm_permission_blocks_handler_when_declined(monkeypatch):
    from velune.cli.repl import VeluneREPL

    repl = VeluneREPL.__new__(VeluneREPL)
    repl._registry = SlashCommandRegistry()
    repl._completer = None
    repl.console = MagicMock()
    repl.container = MagicMock()

    handler = AsyncMock()
    repl._registry.register(
        SlashCommand(
            name="dangerous",
            aliases=[],
            description="d",
            usage="/dangerous",
            handler=handler,
            permissions=("confirm",),
        )
    )
    monkeypatch.setattr(
        "velune.cli.handlers.confirm.confirm_destructive", lambda *a, **k: False
    )

    await repl._handle_slash_command("/dangerous")

    handler.assert_not_awaited()
    repl.console.print.assert_any_call("[dim]Cancelled.[/dim]")


async def test_confirm_permission_runs_handler_when_accepted(monkeypatch):
    from velune.cli.repl import VeluneREPL

    repl = VeluneREPL.__new__(VeluneREPL)
    repl._registry = SlashCommandRegistry()
    repl._completer = None
    repl.console = MagicMock()
    repl.container = MagicMock()

    handler = AsyncMock()
    repl._registry.register(
        SlashCommand(
            name="dangerous",
            aliases=[],
            description="d",
            usage="/dangerous",
            handler=handler,
            permissions=("confirm",),
        )
    )
    monkeypatch.setattr(
        "velune.cli.handlers.confirm.confirm_destructive", lambda *a, **k: True
    )

    await repl._handle_slash_command("/dangerous")

    handler.assert_awaited_once_with("")


async def test_command_without_confirm_permission_runs_unconditionally():
    from velune.cli.repl import VeluneREPL

    repl = VeluneREPL.__new__(VeluneREPL)
    repl._registry = SlashCommandRegistry()
    repl._completer = None
    repl.console = MagicMock()

    handler = AsyncMock()
    repl._registry.register(
        SlashCommand(name="safe", aliases=[], description="d", usage="/safe", handler=handler)
    )

    await repl._handle_slash_command("/safe")

    handler.assert_awaited_once()


def test_confirm_destructive_honors_auto_accept():
    repl = MagicMock()
    repl.container.get.return_value = True  # runtime.auto_accept
    assert confirm_destructive(repl, "  Sure?") is True


async def test_memory_clear_does_nothing_without_confirmation(monkeypatch):
    repl = MagicMock()
    working = MagicMock()
    repl.container.get.side_effect = lambda key: {
        "runtime.working_memory": working,
        "runtime.episodic_memory": MagicMock(),
    }.get(key)
    monkeypatch.setattr(
        "velune.cli.handlers.confirm.confirm_destructive", lambda *a, **k: False
    )

    await cmd_memory(repl, "clear")

    working.clear.assert_not_called()
    repl.console.print.assert_any_call("[dim]Cancelled.[/dim]")


async def test_memory_clear_wipes_working_memory_once_confirmed(monkeypatch):
    repl = MagicMock()
    working = MagicMock()
    repl.container.get.side_effect = lambda key: {
        "runtime.working_memory": working,
        "runtime.episodic_memory": MagicMock(),
    }.get(key)
    monkeypatch.setattr(
        "velune.cli.handlers.confirm.confirm_destructive", lambda *a, **k: True
    )

    await cmd_memory(repl, "clear")

    working.clear.assert_called_once()
