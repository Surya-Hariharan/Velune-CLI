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
    # Who registered the command: "" for a built-in or a user's own command file, a plugin's
    # name for a plugin command. The registry uses it to refuse a clash between owners and to
    # remove everything a plugin registered when it is disabled or reloaded.
    owner: str = ""


#: Names no plugin or command file may take, even though they are not built-in commands.
#: Velune tells users to run ``velune trust ...``; a command named ``trust`` would capture
#: exactly what they are told to type.
RESERVED_COMMAND_NAMES = frozenset({"trust"})


def normalize_command_key(key: str) -> str:
    """The form a command name or alias is stored and looked up under.

    The REPL lower-cases what the user types and splits it on whitespace, so a key is stored
    the same way; a leading ``/`` is dropped so ``"/trust"`` cannot be an alias for ``trust``
    that slips past a check made on ``trust``.
    """
    return key.strip().lstrip("/").lower()


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._owners: dict[str, str] = {}
        self._sealed: frozenset[str] = frozenset()
        self._is_sealed = False

    def seal_builtins(self) -> None:
        """Protect every name and alias registered so far.

        Called once the built-in commands are in. From then on nothing can register one of
        those keys (or a reserved name), whatever order things are registered in and
        however the key is spelled, so a plugin or a command file cannot shadow a built-in.
        """
        self._sealed = frozenset(self._commands)
        self._is_sealed = True

    def _claim(self, key: str, cmd: SlashCommand) -> bool:
        """Bind *key* to *cmd*. Returns False (and logs why) when the key is protected.

        Before :meth:`seal_builtins`, the last registration wins with a warning (two built-ins
        fighting over a key is a bug worth surfacing). After it, a protected key is refused,
        and a key already held by a *different owner* is refused too (the first one wins), so
        the outcome does not depend on anything but registration order.
        """
        normalized = normalize_command_key(key)
        if not normalized or any(ch.isspace() for ch in normalized):
            _log.warning("Ignoring command key %r for /%s: it cannot be typed.", key, cmd.name)
            return False
        if self._is_sealed:
            if normalized in self._sealed or normalized in RESERVED_COMMAND_NAMES:
                _log.warning(
                    "Refusing command key %r for /%s: it is a protected built-in or reserved name.",
                    key,
                    cmd.name,
                )
                return False
            holder = self._owners.get(normalized)
            existing = self._commands.get(normalized)
            if existing is not None and holder != cmd.owner:
                _log.warning(
                    "Refusing command key %r for /%s: already registered by %s.",
                    key,
                    cmd.name,
                    f"plugin '{holder}'" if holder else "another command",
                )
                return False
        existing = self._commands.get(normalized)
        if existing is not None and existing.name != cmd.name and not self._is_sealed:
            _log.warning(
                "Slash command key %r already bound to /%s; overriding with /%s",
                key,
                existing.name,
                cmd.name,
            )
        self._commands[normalized] = cmd
        self._owners[normalized] = cmd.owner
        return True

    def register(self, cmd: SlashCommand) -> list[str]:
        """Register *cmd* under its name and aliases; returns the keys that were refused.

        A refused name refuses the whole command (an alias must not make an attacker's
        command reachable when its own name was turned down); a refused alias only drops
        that alias.
        """
        refused: list[str] = []
        if not self._claim(cmd.name, cmd):
            return [cmd.name]
        for alias in cmd.aliases:
            if not self._claim(alias, cmd):
                refused.append(alias)
        return refused

    def unregister_owner(self, owner: str) -> list[str]:
        """Remove everything *owner* registered; returns the removed keys. Idempotent."""
        if not owner:
            return []
        keys = [key for key, holder in self._owners.items() if holder == owner]
        for key in keys:
            self._commands.pop(key, None)
            self._owners.pop(key, None)
        return keys

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(normalize_command_key(name))

    def all_unique(self) -> list[SlashCommand]:
        seen: set[str] = set()
        result: list[SlashCommand] = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(cmd)
        return sorted(result, key=lambda c: c.name)
