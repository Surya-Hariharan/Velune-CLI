"""Slash command registry for the Velune REPL."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_log = logging.getLogger("velune.cli.slash_commands")


@dataclass
class SlashCommand:
    name: str
    aliases: list[str]
    description: str
    usage: str
    handler: Callable[..., Awaitable[None]]
    # Category drives grouping in /help and the completion menu. It lives on the
    # command itself (not a separate dict) so the two can never drift. Defaults
    # to "General"; built-ins are assigned in slash_dispatcher.
    category: str = "General"
    # Hidden developer commands are omitted from /help unless `/help --all`.
    hidden: bool = False
    # Optional palette metadata. Commands without explicit examples still show
    # their usage as a useful, deterministic example.
    examples: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    shortcut: str | None = None
    # Free-form permission tags gating dispatch (see
    # VeluneREPL._handle_slash_command). Only "confirm" is currently
    # interpreted — it asks the user (honoring --yes/auto-accept) before the
    # handler runs. Use this only for commands that are destructive in their
    # entirety; a command whose destructive behavior lives behind one
    # subcommand (e.g. "/memory clear" vs. "/memory stats") should confirm
    # inline in its handler instead, via velune.cli.handlers.confirm.
    permissions: tuple[str, ...] = ()


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def _claim(self, key: str, cmd: SlashCommand) -> None:
        """Bind *key* to *cmd*, warning if it silently shadows a different command.

        Last-write-wins would otherwise let two commands quietly fight over the
        same name or alias (e.g. ``/h`` mapping to both ``/help`` and
        ``/history``). Surfacing the clash turns a silent UX bug into a visible
        warning that tests and developers can catch.
        """
        existing = self._commands.get(key)
        if existing is not None and existing.name != cmd.name:
            _log.warning(
                "Slash command key %r already bound to /%s; overriding with /%s",
                key,
                existing.name,
                cmd.name,
            )
        self._commands[key] = cmd

    def register(self, cmd: SlashCommand) -> None:
        self._claim(cmd.name, cmd)
        for alias in cmd.aliases:
            self._claim(alias, cmd)

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name.lower())

    def all_unique(self) -> list[SlashCommand]:
        seen: set[str] = set()
        result: list[SlashCommand] = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(cmd)
        return sorted(result, key=lambda c: c.name)
