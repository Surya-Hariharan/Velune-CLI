"""Regression tests for the slash-command registry: alias integrity, the
cognition→index rename, mode consolidation, and /help categorization.

These lock in the UX fixes so a future edit that reintroduces an alias
collision (e.g. /h mapping to both /help and /history) fails loudly.
"""

from __future__ import annotations

import logging

import pytest

from velune.cli.autocomplete import CATEGORY_ORDER, COMMAND_CATEGORIES
from velune.cli.slash_commands import SlashCommand, SlashCommandRegistry
from velune.cli.slash_dispatcher import build_slash_registry


class _StubContainer:
    def get(self, key):  # noqa: ANN001
        return None


class _StubREPL:
    """Minimal stand-in: every ``_cmd_*`` handler resolves to an async no-op."""

    def __init__(self) -> None:
        self.container = _StubContainer()
        self.console = None

    def __getattr__(self, name):  # noqa: ANN001
        async def _handler(args: str = "") -> None:
            return None

        return _handler


@pytest.fixture
def registry() -> SlashCommandRegistry:
    return build_slash_registry(_StubREPL())


def _canonical(registry: SlashCommandRegistry, key: str) -> str | None:
    cmd = registry.get(key)
    return cmd.name if cmd else None


class TestAliasIntegrity:
    def test_no_alias_collisions(self, registry: SlashCommandRegistry) -> None:
        """Every name and alias must map to exactly one command."""
        seen: dict[str, str] = {}
        collisions: list[str] = []
        for cmd in registry.all_unique():
            for key in (cmd.name, *cmd.aliases):
                if key in seen and seen[key] != cmd.name:
                    collisions.append(f"{key}: /{seen[key]} vs /{cmd.name}")
                seen[key] = cmd.name
        assert collisions == [], f"alias collisions: {collisions}"

    def test_h_resolves_to_help_not_history(self, registry: SlashCommandRegistry) -> None:
        assert _canonical(registry, "h") == "help"
        assert _canonical(registry, "hist") == "history"

    def test_status_alias_freed(self, registry: SlashCommandRegistry) -> None:
        # /status no longer shadows /mode (it clashed with the shell's
        # `velune status`), so it should resolve to nothing.
        assert registry.get("status") is None

    def test_register_warns_on_collision(self, caplog) -> None:  # noqa: ANN001
        async def _noop(args: str = "") -> None:
            return None

        reg = SlashCommandRegistry()
        reg.register(SlashCommand("help", ["x"], "", "", _noop))
        with caplog.at_level(logging.WARNING, logger="velune.cli.slash_commands"):
            reg.register(SlashCommand("history", ["x"], "", "", _noop))
        assert any("already bound" in m for m in caplog.messages)


class TestCognitionRename:
    def test_index_is_canonical(self, registry: SlashCommandRegistry) -> None:
        assert _canonical(registry, "index") == "index"

    def test_cognition_is_backcompat_alias(self, registry: SlashCommandRegistry) -> None:
        assert _canonical(registry, "cognition") == "index"
        assert _canonical(registry, "cog") == "index"


class TestModeConsolidation:
    def test_mode_has_no_status_alias(self, registry: SlashCommandRegistry) -> None:
        assert registry.get("mode").aliases == []

    def test_legacy_mode_commands_still_exist(self, registry: SlashCommandRegistry) -> None:
        # Back-compat: the old standalone mode commands still resolve.
        assert _canonical(registry, "optimus") == "optimus"
        assert _canonical(registry, "godly") == "godly"
        assert _canonical(registry, "normal") == "normal"


class TestHelpCategorization:
    def test_every_command_has_a_category(self, registry: SlashCommandRegistry) -> None:
        uncategorized = [
            cmd.name for cmd in registry.all_unique() if cmd.name not in COMMAND_CATEGORIES
        ]
        assert uncategorized == [], f"commands missing a category: {uncategorized}"

    def test_all_categories_are_ordered(self, registry: SlashCommandRegistry) -> None:
        used = {COMMAND_CATEGORIES[cmd.name] for cmd in registry.all_unique()}
        assert used <= set(CATEGORY_ORDER), f"categories outside CATEGORY_ORDER: {used}"
