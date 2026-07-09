"""Tests for recently-used commands surfacing in the command palette.

Shares SlashCompleter's existing recency deque (velune.cli.autocomplete)
rather than duplicating a second tracker, so Tab-completion and the palette
agree on "recently used" from one source of truth.
"""

from __future__ import annotations

from velune.cli.autocomplete import CommandEntry, SlashCompleter
from velune.cli.command_palette import CommandPalette
from velune.cli.slash_commands import SlashCommand


async def _noop(args: str) -> None:
    pass


def _commands():
    return [
        SlashCommand(name="help", aliases=[], description="d", usage="/help", handler=_noop, category="General"),
        SlashCommand(name="memory", aliases=["mem"], description="d", usage="/memory", handler=_noop, category="Memory"),
        SlashCommand(name="backup", aliases=[], description="d", usage="/backup", handler=_noop, category="Session"),
        SlashCommand(name="mcp", aliases=[], description="d", usage="/mcp", handler=_noop, category="Tools"),
    ]


def test_empty_query_view_has_no_recent_group_when_nothing_used_yet():
    palette = CommandPalette(_commands())
    matches = palette.model.matches("")
    assert all(m.group != "Recent" for m in matches)


def test_recently_used_commands_surface_first_in_recency_order():
    recent = ["mcp", "backup"]
    palette = CommandPalette(_commands(), recency_source=lambda: recent)

    matches = palette.model.matches("")

    recent_matches = [m for m in matches if m.group == "Recent"]
    assert [m.command.name for m in recent_matches] == ["mcp", "backup"]
    # Every command still appears exactly once overall.
    assert sorted(m.command.name for m in matches) == ["backup", "help", "mcp", "memory"]


def test_active_search_ignores_recency_grouping():
    """Recency only reshapes the browsing (empty-query) view — an active
    search still groups by real category so results stay predictable."""
    recent = ["mcp"]
    palette = CommandPalette(_commands(), recency_source=lambda: recent)

    matches = palette.model.matches("mem")

    assert all(m.group != "Recent" for m in matches)
    assert [m.command.name for m in matches] == ["memory"]


def test_palette_shares_the_completer_recency_source():
    completer = SlashCompleter(
        commands=[CommandEntry(name="mcp", description="d"), CommandEntry(name="backup", description="d")],
        show_command_completions=False,
    )
    completer.record_use("mcp")
    completer.record_use("backup")
    completer.record_use("mcp")  # used again — should move back to the front

    palette = CommandPalette(_commands(), recency_source=completer.recent_commands)
    matches = palette.model.matches("")

    recent_names = [m.command.name for m in matches if m.group == "Recent"]
    assert recent_names == ["mcp", "backup"]


def test_recency_source_errors_degrade_to_no_recent_group():
    def _broken():
        raise RuntimeError("boom")

    palette = CommandPalette(_commands(), recency_source=_broken)
    matches = palette.model.matches("")  # must not raise

    assert all(m.group != "Recent" for m in matches)
