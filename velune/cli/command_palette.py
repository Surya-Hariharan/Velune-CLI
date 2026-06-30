"""Keyboard-first floating command palette for the interactive REPL.

The palette deliberately reads from :class:`SlashCommandRegistry`; it is a
view over executable commands, not a second command catalogue. The model is
kept separate from prompt_toolkit rendering so fuzzy ranking and selection are
cheap to test without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from velune.cli.autocomplete import CATEGORY_ORDER, fuzzy_score
from velune.cli.slash_commands import SlashCommand

if TYPE_CHECKING:
    from prompt_toolkit.shortcuts import PromptSession


@dataclass(frozen=True, slots=True)
class PaletteMatch:
    command: SlashCommand
    score: int


class CommandPaletteModel:
    """Fuzzy command search, grouping, and keyboard selection state."""

    def __init__(self, commands: list[SlashCommand]) -> None:
        self.commands: list[SlashCommand] = []
        self.selected_index = 0
        self._last_query: str | None = None
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
    def _field_score(query: str, command: SlashCommand) -> int:
        if not query:
            return 1

        # Command names and aliases are intent-bearing, so they outrank a
        # coincidental match in prose. Search terms let domain words such as
        # "anthropic" lead to the provider-management command.
        name_score = fuzzy_score(query, command.name) * 5
        alias_score = max(
            (fuzzy_score(query, alias) * 5 for alias in command.aliases),
            default=0,
        )
        term_score = max(
            (fuzzy_score(query, term) * 4 for term in command.search_terms),
            default=0,
        )
        detail_score = max(
            fuzzy_score(query, command.description),
            fuzzy_score(query, command.usage),
            fuzzy_score(query, command.category),
        )
        return max(name_score, alias_score, term_score, detail_score)

    def matches(self, query: str) -> list[PaletteMatch]:
        if query != self._last_query:
            self.selected_index = 0
            self._last_query = query

        scored = [
            PaletteMatch(command=command, score=self._field_score(query, command))
            for command in self.commands
        ]
        scored = [match for match in scored if match.score > 0]

        category_rank = {name: index for index, name in enumerate(CATEGORY_ORDER)}
        if not query:
            scored.sort(
                key=lambda match: (
                    category_rank.get(match.command.category, len(CATEGORY_ORDER)),
                    match.command.category,
                    match.command.name,
                )
            )
        else:
            # Keep results grouped while ordering the groups themselves by
            # their strongest match. This preserves scanning structure without
            # hiding the most relevant result below a fixed category order.
            group_score: dict[str, int] = {}
            for match in scored:
                group_score[match.command.category] = max(
                    group_score.get(match.command.category, 0), match.score
                )
            scored.sort(
                key=lambda match: (
                    -group_score[match.command.category],
                    category_rank.get(match.command.category, len(CATEGORY_ORDER)),
                    match.command.category,
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

    def __init__(self, commands: list[SlashCommand]) -> None:
        self.model = CommandPaletteModel(commands)
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
        previous_category: str | None = None
        for offset, match in enumerate(visible):
            index = start + offset
            command = match.command
            if command.category != previous_category:
                if previous_category is not None:
                    lines.append(("", "\n"))
                lines.append(("class:palette.group", f"  {command.category.upper()}\n"))
                previous_category = command.category
            selected = index == self.model.selected_index
            marker = ">" if selected else " "
            style = "class:palette.selected" if selected else "class:palette.command"
            # Name + a short description so the grouped, empty-query view reads as a
            # browseable feature menu, not just a list of command names.
            name_cell = f"/{command.name}"
            desc = command.description
            if len(desc) > 52:
                desc = desc[:51].rstrip() + "…"
            lines.append((style, f"  {marker} {name_cell:<16}"))
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
                    "  Up/Down navigate  Enter run  Tab complete  Esc close",
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
