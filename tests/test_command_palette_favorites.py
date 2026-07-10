"""Tests for pinned favorites in the command palette.

Favorites are persisted via FavoritesStore (a JSON file) and surface as their
own group above Recent in the browsing (empty-query) view. Palettes built
without a store keep favorites disabled — no surprise disk reads in tests or
minimal embeds.
"""

from __future__ import annotations

from velune.cli.command_palette import CommandPalette, FavoritesStore
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


def _store(tmp_path, names=()) -> FavoritesStore:
    store = FavoritesStore(path=tmp_path / "favorites.json")
    for name in names:
        store.toggle(name)
    return store


# ── Store persistence ────────────────────────────────────────────────────────


def test_toggle_pins_and_unpins_with_persistence(tmp_path):
    path = tmp_path / "favorites.json"
    store = FavoritesStore(path=path)
    assert store.toggle("mcp") is True
    assert store.toggle("backup") is True
    assert store.toggle("mcp") is False  # unpin

    # A fresh store re-reads what was persisted.
    assert FavoritesStore(path=path).names() == {"backup"}


def test_corrupt_favorites_file_degrades_to_empty(tmp_path):
    path = tmp_path / "favorites.json"
    path.write_text("{not json", encoding="utf-8")
    assert FavoritesStore(path=path).names() == set()


# ── Browsing view grouping ───────────────────────────────────────────────────


def test_favorites_group_first_above_recent(tmp_path):
    palette = CommandPalette(
        _commands(),
        recency_source=lambda: ["backup"],
        favorites=_store(tmp_path, ["mcp"]),
    )
    matches = palette.model.matches("")
    groups = [m.group for m in matches]
    assert groups[0] == "Favorites"
    assert matches[0].command.name == "mcp"
    assert groups.index("Favorites") < groups.index("Recent")
    # Every command still appears exactly once overall.
    assert sorted(m.command.name for m in matches) == ["backup", "help", "mcp", "memory"]


def test_favorited_command_leaves_recent_group(tmp_path):
    palette = CommandPalette(
        _commands(),
        recency_source=lambda: ["mcp", "backup"],
        favorites=_store(tmp_path, ["mcp"]),
    )
    matches = palette.model.matches("")
    recent = [m.command.name for m in matches if m.group == "Recent"]
    assert recent == ["backup"]


def test_active_search_ignores_favorites_grouping(tmp_path):
    palette = CommandPalette(_commands(), favorites=_store(tmp_path, ["memory"]))
    matches = palette.model.matches("mem")
    assert all(m.group != "Favorites" for m in matches)


def test_no_store_means_no_favorites_group():
    palette = CommandPalette(_commands())
    matches = palette.model.matches("")
    assert all(m.group != "Favorites" for m in matches)


# ── Rendering ────────────────────────────────────────────────────────────────


def test_render_marks_favorites_with_star(tmp_path, monkeypatch):
    palette = CommandPalette(_commands(), favorites=_store(tmp_path, ["mcp"]))
    monkeypatch.setattr(palette, "_buffer_text", lambda: "/")
    text = "".join(t for _s, t in palette.render_commands())
    starred = [line for line in text.split("\n") if "★" in line]
    assert len(starred) == 1
    assert "/mcp" in starred[0]


def test_details_footer_advertises_pin_shortcut_only_with_store(tmp_path, monkeypatch):
    palette = CommandPalette(_commands(), favorites=_store(tmp_path))
    monkeypatch.setattr(palette, "_buffer_text", lambda: "/help")
    text = "".join(t for _s, t in palette.render_details())
    assert "Ctrl+F pin" in text

    bare = CommandPalette(_commands())
    monkeypatch.setattr(bare, "_buffer_text", lambda: "/help")
    text = "".join(t for _s, t in bare.render_details())
    assert "Ctrl+F" not in text
