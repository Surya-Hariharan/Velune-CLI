"""Keyboard-first floating command palette for the interactive REPL.

The palette deliberately reads from :class:`SlashCommandRegistry`; it is a
view over executable commands, not a second command catalogue. The model is
kept separate from prompt_toolkit rendering so fuzzy ranking and selection are
cheap to test without a terminal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from velune.cli import design
from velune.cli.autocomplete import CATEGORY_ORDER
from velune.cli.slash_commands import SlashCommand

if TYPE_CHECKING:
    from prompt_toolkit.shortcuts import PromptSession


_RECENT_GROUP = "Recent"
_FAVORITES_GROUP = "Favorites"
_MAX_RECENT_SHOWN = 5


class FavoritesStore:
    """Persistent set of pinned command names (~/.velune/palette_favorites.json).

    All disk IO is best-effort: a corrupt or unwritable file degrades to
    "no favorites", never to a broken palette.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (Path.home() / ".velune" / "palette_favorites.json")
        self._names: set[str] | None = None

    def names(self) -> set[str]:
        if self._names is None:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._names = {str(n) for n in data} if isinstance(data, list) else set()
            except Exception:
                self._names = set()
        return self._names

    def toggle(self, name: str) -> bool:
        """Pin/unpin *name*; returns True when it is now pinned."""
        names = self.names()
        pinned = name not in names
        if pinned:
            names.add(name)
        else:
            names.discard(name)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(sorted(names)), encoding="utf-8")
        except Exception:
            pass
        return pinned


@dataclass(frozen=True, slots=True)
class PaletteMatch:
    command: SlashCommand
    score: int
    # Display/sort group. Usually command.category, but the browsing
    # (empty-query) view breaks recently-used commands out into their own
    # "Recent" group at the top — see CommandPaletteModel.matches().
    group: str


class CommandPaletteModel:
    """Fuzzy command search, grouping, and keyboard selection state."""

    def __init__(
        self,
        commands: list[SlashCommand],
        recency_source: Callable[[], list[str]] | None = None,
        favorites_source: Callable[[], set[str]] | None = None,
    ) -> None:
        self.commands: list[SlashCommand] = []
        self.selected_index = 0
        self._last_query: str | None = None
        # Optional callable returning most-recent-first command names, shared
        # with SlashCompleter (velune.cli.autocomplete) so Tab-completion and
        # the palette agree on "recently used" from one source of truth.
        self._recency_source = recency_source
        # Optional callable returning pinned command names (FavoritesStore).
        self._favorites_source = favorites_source
        self.set_commands(commands)

    def set_commands(self, commands: list[SlashCommand]) -> None:
        self.commands = [cmd for cmd in commands if not cmd.hidden]
        self.selected_index = 0
        self._last_query = None

    @staticmethod
    def query_from_text(text: str) -> str | None:
        """Return a palette query for a slash-prefixed command head."""
        if not text.startswith("/") or any(ch.isspace() for ch in text[1:]):
            return None
        return text[1:]

    @staticmethod
    def _contains_score(query: str, candidate: str, *, allow_substring: bool = True) -> int:
        """Exact/prefix[/substring] match only — no letter-scatter subsequence tier.

        Search terms are short curated keywords ("credentials", "background"),
        not identifiers a user is abbreviating. Subsequence matching against
        them finds the query's letters scattered in order almost anywhere,
        which is coincidental noise rather than a real match. Command names
        and aliases go further and disable the substring tier too
        (``allow_substring=False``): a query of "c" must only surface commands
        that *start with* "c", not ones like /doctor or /mcp that merely
        contain a "c" somewhere in the middle.
        """
        query = query.lower()
        candidate_l = candidate.lower()
        if query == candidate_l:
            return 1000
        if candidate_l.startswith(query):
            return 500 - len(candidate_l)
        if allow_substring and query in candidate_l:
            return 250 - candidate_l.index(query)
        return 0

    @classmethod
    def _field_score(cls, query: str, command: SlashCommand) -> int:
        if not query:
            return 1

        # Matching is scoped to intent-bearing fields only: the command name,
        # its aliases, and its curated search terms (domain words such as
        # "anthropic" that should lead to the provider-management command).
        # Description/usage/category text is prose, not deliberately chosen
        # search vocabulary — fuzzy-matching against it let a typed query like
        # "coun" surface /godly, /run, and /model purely because those long
        # strings happened to contain the right letters somewhere, burying the
        # commands that actually start with "coun" among unrelated ones.
        name_score = cls._contains_score(query, command.name, allow_substring=False) * 5
        alias_score = max(
            (
                cls._contains_score(query, alias, allow_substring=False) * 5
                for alias in command.aliases
            ),
            default=0,
        )
        term_score = max(
            (cls._contains_score(query, term) * 4 for term in command.search_terms),
            default=0,
        )
        return max(name_score, alias_score, term_score)

    def _recent_names(self) -> list[str]:
        if self._recency_source is None:
            return []
        try:
            return self._recency_source()[:_MAX_RECENT_SHOWN]
        except Exception:
            return []

    def favorite_names(self) -> set[str]:
        if self._favorites_source is None:
            return set()
        try:
            return set(self._favorites_source())
        except Exception:
            return set()

    def matches(self, query: str) -> list[PaletteMatch]:
        if query != self._last_query:
            self.selected_index = 0
            self._last_query = query

        category_rank = {name: index for index, name in enumerate(CATEGORY_ORDER)}

        if not query:
            # Browsing view: pinned favorites first, then recently-used, then
            # the rest grouped by category. A command appears in exactly one
            # group (favorites win over recents).
            favorite_names = self.favorite_names()
            recent_names = [n for n in self._recent_names() if n not in favorite_names]
            recent_rank = {name: i for i, name in enumerate(recent_names)}
            by_name = {cmd.name: cmd for cmd in self.commands}

            favorite_matches = [
                PaletteMatch(command=by_name[name], score=1, group=_FAVORITES_GROUP)
                for name in sorted(favorite_names)
                if name in by_name
            ]
            recent_matches = [
                PaletteMatch(command=by_name[name], score=1, group=_RECENT_GROUP)
                for name in recent_names
                if name in by_name
            ]
            rest_matches = [
                PaletteMatch(command=cmd, score=1, group=cmd.category)
                for cmd in self.commands
                if cmd.name not in recent_rank and cmd.name not in favorite_names
            ]
            rest_matches.sort(
                key=lambda match: (
                    category_rank.get(match.group, len(CATEGORY_ORDER)),
                    match.group,
                    match.command.name,
                )
            )
            scored = favorite_matches + recent_matches + rest_matches
        else:
            scored = [
                PaletteMatch(
                    command=command, score=self._field_score(query, command), group=command.category
                )
                for command in self.commands
            ]
            scored = [match for match in scored if match.score > 0]

            # Keep results grouped while ordering the groups themselves by
            # their strongest match. This preserves scanning structure without
            # hiding the most relevant result below a fixed category order.
            group_score: dict[str, int] = {}
            for match in scored:
                group_score[match.group] = max(group_score.get(match.group, 0), match.score)
            scored.sort(
                key=lambda match: (
                    -group_score[match.group],
                    category_rank.get(match.group, len(CATEGORY_ORDER)),
                    match.group,
                    -match.score,
                    match.command.name,
                )
            )

        if scored:
            self.selected_index = min(self.selected_index, len(scored) - 1)
        else:
            self.selected_index = 0
        return scored

    def move(self, query: str, amount: int) -> None:
        matches = self.matches(query)
        if matches:
            self.selected_index = (self.selected_index + amount) % len(matches)

    def selected(self, query: str) -> SlashCommand | None:
        matches = self.matches(query)
        if not matches:
            return None
        return matches[self.selected_index].command


class CommandPalette:
    """prompt_toolkit renderer and key bindings for :class:`CommandPaletteModel`."""

    def __init__(
        self,
        commands: list[SlashCommand],
        recency_source: Callable[[], list[str]] | None = None,
        favorites: FavoritesStore | None = None,
    ) -> None:
        # No default store: tests and callers that don't pass one get a
        # palette with favorites disabled instead of surprise disk reads.
        self._favorites = favorites
        self.model = CommandPaletteModel(
            commands,
            recency_source=recency_source,
            favorites_source=favorites.names if favorites else None,
        )
        self._dismissed_text: str | None = None

    def set_commands(self, commands: list[SlashCommand]) -> None:
        self.model.set_commands(commands)

    def _buffer_text(self) -> str:
        try:
            return get_app().current_buffer.text
        except Exception:
            return ""

    def query(self) -> str | None:
        return self.model.query_from_text(self._buffer_text())

    def is_active(self) -> bool:
        text = self._buffer_text()
        return self.model.query_from_text(text) is not None and text != self._dismissed_text

    def dismiss(self) -> None:
        self._dismissed_text = self._buffer_text()

    def _selected(self) -> SlashCommand | None:
        query = self.query()
        return self.model.selected(query) if query is not None else None

    @staticmethod
    def _window_start(total: int, selected: int, limit: int) -> int:
        if total <= limit:
            return 0
        return min(max(0, selected - limit // 2), total - limit)

    def render_commands(self) -> FormattedText:
        query = self.query() or ""
        matches = self.model.matches(query)
        count = len(matches)
        lines: list[tuple[str, str]] = [
            ("class:palette.label", "  SEARCH\n"),
            ("class:palette.query", f"  /{query}"),
            ("class:palette.muted", f"  {count} result{'s' if count != 1 else ''}\n\n"),
        ]
        if not matches:
            lines.append(("class:palette.warning", "  No matching commands\n"))
            return FormattedText(lines)

        limit = 9
        start = self._window_start(count, self.model.selected_index, limit)
        visible = matches[start : start + limit]
        favorite_names = self.model.favorite_names()
        previous_group: str | None = None
        for offset, match in enumerate(visible):
            index = start + offset
            command = match.command
            if match.group != previous_group:
                if previous_group is not None:
                    lines.append(("", "\n"))
                lines.append(("class:palette.group", f"  {match.group.upper()}\n"))
                previous_group = match.group
            selected = index == self.model.selected_index
            marker = ">" if selected else " "
            star = "★" if command.name in favorite_names else " "
            style = "class:palette.selected" if selected else "class:palette.command"
            # Name + a short description so the grouped, empty-query view reads as a
            # browseable feature menu, not just a list of command names.
            name_cell = f"/{command.name}"
            desc = command.description
            if len(desc) > 52:
                desc = desc[:51].rstrip() + "…"
            lines.append((style, f" {marker}{star} {name_cell:<16}"))
            lines.append(("class:palette.muted", f"{desc}\n"))
        return FormattedText(lines)

    def render_details(self) -> FormattedText:
        command = self._selected()
        if command is None:
            return FormattedText([("class:palette.muted", "  Keep typing to refine your search.")])

        examples = command.examples or (command.usage,)
        shortcut = command.shortcut
        if shortcut is None and command.aliases:
            shortcut = f"/{min(command.aliases, key=len)}"
        shortcut = shortcut or "Enter"

        lines: list[tuple[str, str]] = [
            ("class:palette.title", f"  /{command.name}\n"),
            ("class:palette.muted", f"  {command.category}\n\n"),
            ("class:palette.label", "  DESCRIPTION\n"),
            ("class:palette.text", f"  {command.description}\n\n"),
            ("class:palette.label", "  USAGE\n"),
            ("class:palette.code", f"  {command.usage}\n\n"),
            ("class:palette.label", "  EXAMPLES\n"),
        ]
        for example in examples[:3]:
            lines.append(("class:palette.code", f"  {example}\n"))
        lines.extend(
            [
                ("", "\n"),
                ("class:palette.label", "  SHORTCUT\n"),
                ("class:palette.code", f"  {shortcut}\n\n"),
                (
                    "class:palette.muted",
                    "  Up/Down navigate  Enter run  Tab complete  "
                    + ("Ctrl+F pin  " if self._favorites else "")
                    + "Esc close",
                ),
            ]
        )
        return FormattedText(lines)

    def add_bindings(self, bindings: KeyBindings) -> None:
        active = Condition(self.is_active)

        @bindings.add("up", filter=active, eager=True)
        def _up(event) -> None:
            self.model.move(self.query() or "", -1)
            event.app.invalidate()

        @bindings.add("down", filter=active, eager=True)
        def _down(event) -> None:
            self.model.move(self.query() or "", 1)
            event.app.invalidate()

        @bindings.add("tab", filter=active, eager=True)
        def _complete(event) -> None:
            command = self._selected()
            if command is None:
                return
            event.current_buffer.text = f"/{command.name} "
            event.current_buffer.cursor_position = len(event.current_buffer.text)
            self._dismissed_text = None

        @bindings.add("enter", filter=active, eager=True)
        def _run(event) -> None:
            command = self._selected()
            if command is None:
                return
            event.current_buffer.text = f"/{command.name}"
            event.current_buffer.cursor_position = len(event.current_buffer.text)
            self._dismissed_text = None
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", filter=active, eager=True)
        def _close(event) -> None:
            self.dismiss()
            event.app.invalidate()

        if self._favorites is not None:

            @bindings.add("c-f", filter=active, eager=True)
            def _toggle_favorite(event) -> None:
                command = self._selected()
                if command is None:
                    return
                self._favorites.toggle(command.name)
                event.app.invalidate()

    def container(self) -> ConditionalContainer:
        left = Window(
            content=FormattedTextControl(self.render_commands),
            width=Dimension(min=25, preferred=34),
            dont_extend_width=False,
        )
        divider = Window(width=1, char="|", style="class:palette.border")
        right = Window(
            content=FormattedTextControl(self.render_details),
            width=Dimension(min=30, weight=2),
        )
        body = VSplit([left, divider, right], padding=1, padding_style="class:palette.border")
        frame = Frame(
            body,
            title=[("class:palette.frame-title", " COMMAND PALETTE ")],
            style="class:palette.frame",
        )
        return ConditionalContainer(frame, filter=Condition(self.is_active))

    def attach(self, session: PromptSession) -> None:
        """Overlay the palette on a PromptSession without replacing its buffer."""
        root = session.layout.container
        session.layout.container = FloatContainer(
            content=root,
            floats=[
                Float(
                    content=self.container(),
                    left=2,
                    right=2,
                    top=1,
                    height=18,
                    allow_cover_cursor=True,
                    z_index=20,
                )
            ],
        )


PALETTE_STYLES: dict[str, str] = {
    "palette.frame": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "palette.frame-title": f"bg:{design.SURFACE} fg:{design.ACCENT} bold",
    "palette.border": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "palette.title": f"bg:{design.SURFACE} fg:{design.WHITE} bold",
    "palette.label": f"bg:{design.SURFACE} fg:{design.MUTED} bold",
    "palette.query": f"bg:{design.SURFACE} fg:{design.ACCENT} bold",
    "palette.group": f"bg:{design.SURFACE} fg:{design.MUTED} bold",
    "palette.command": f"bg:{design.SURFACE} fg:{design.WHITE}",
    "palette.selected": f"bg:{design.LIGHT_BG} fg:{design.ACCENT_SOFT} bold",
    "palette.text": f"bg:{design.SURFACE} fg:{design.WHITE}",
    "palette.code": f"bg:{design.SURFACE} fg:{design.ACCENT_SOFT}",
    "palette.muted": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "palette.warning": f"bg:{design.SURFACE} fg:{design.WARN}",
}
