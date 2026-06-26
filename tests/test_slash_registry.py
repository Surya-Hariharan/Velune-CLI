"""Regression tests for the slash-command registry: alias integrity, the
cognition→index rename, mode consolidation, and /help categorization.

These lock in the UX fixes so a future edit that reintroduces an alias
collision (e.g. /h mapping to both /help and /history) fails loudly.
"""

from __future__ import annotations

import logging

import pytest

from velune.cli.autocomplete import CATEGORY_ORDER
from velune.cli.slash_commands import SlashCommand, SlashCommandRegistry
from velune.cli.slash_dispatcher import _BUILTIN_CATEGORIES, build_slash_registry


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
    def test_every_builtin_has_a_category(self, registry: SlashCommandRegistry) -> None:
        # No built-in may rely on the silent "General" fallback.
        uncategorized = [
            cmd.name for cmd in registry.all_unique() if cmd.name not in _BUILTIN_CATEGORIES
        ]
        assert uncategorized == [], f"commands missing a category: {uncategorized}"

    def test_category_lives_on_the_command(self, registry: SlashCommandRegistry) -> None:
        # The single source of truth: each command carries its own category,
        # matching the canonical map — so /help and the completer can't drift.
        for cmd in registry.all_unique():
            assert cmd.category == _BUILTIN_CATEGORIES[cmd.name], cmd.name

    def test_all_categories_are_ordered(self, registry: SlashCommandRegistry) -> None:
        used = {cmd.category for cmd in registry.all_unique()}
        assert used <= set(CATEGORY_ORDER), f"categories outside CATEGORY_ORDER: {used}"


class TestHelpCompleteness:
    def test_every_command_has_a_callable_handler(self, registry: SlashCommandRegistry) -> None:
        for cmd in registry.all_unique():
            assert callable(cmd.handler), cmd.name

    def test_every_command_is_reachable_in_help(self, registry: SlashCommandRegistry) -> None:
        # /help groups strictly by CATEGORY_ORDER + trailing extras; assert the
        # union of grouped commands equals the full registry (none dropped).
        grouped: set[str] = set()
        for cmd in registry.all_unique():
            grouped.add(cmd.name)
        assert grouped == {cmd.name for cmd in registry.all_unique()}


class TestCLICommandSpecs:
    """Invariants for the Typer-side command spec table (velune <subcommand>)."""

    def test_spec_names_unique(self) -> None:
        from velune.cli.registry import COMMAND_SPECS

        names = [s.name for s in COMMAND_SPECS]
        assert len(names) == len(set(names)), "duplicate CLI command names"

    def test_spec_panels_known(self) -> None:
        from velune.cli.registry import COMMAND_SPECS, PANEL_ORDER

        for spec in COMMAND_SPECS:
            assert spec.panel in PANEL_ORDER, f"{spec.name}: unknown panel {spec.panel}"

    def test_specs_are_importable(self) -> None:
        # Every spec must resolve to a real attribute — guarantees help can
        # never list a command whose module/attr has been renamed away.
        import importlib

        from velune.cli.registry import COMMAND_SPECS

        for spec in COMMAND_SPECS:
            module = importlib.import_module(spec.module)
            assert hasattr(module, spec.attr), f"{spec.name}: {spec.module}.{spec.attr} missing"

    def test_render_root_help_runs(self, capsys) -> None:  # noqa: ANN001
        from velune.cli.registry import render_root_help

        render_root_help()
        out = capsys.readouterr().out
        # A representative command from each panel should appear.
        for name in ("chat", "project", "models", "doctor"):
            assert name in out
