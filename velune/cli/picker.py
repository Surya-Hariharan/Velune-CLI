"""Reusable interactive list picker for the Velune REPL.

A lightweight prompt_toolkit application: type to fuzzy-filter, ↑↓ to move,
Enter to select, Esc to cancel. Items can carry an optional group label so
lists render in sections (e.g. sessions grouped by project). Used by the
``/session`` and ``/project`` pickers; the model picker predates this and
keeps its bespoke hardware-fit annotations.
"""

from __future__ import annotations

from dataclasses import dataclass

from velune.cli.autocomplete import fuzzy_score


@dataclass(slots=True)
class PickItem:
    """One selectable row: *label* is matched and shown, *meta* is dim detail."""

    id: str
    label: str
    meta: str = ""
    group: str | None = None
    is_current: bool = False


async def pick(title: str, items: list[PickItem]) -> PickItem | None:
    """Run the interactive picker; returns the chosen item or None on cancel."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not items:
        return None

    selected_index = [0]
    filter_text = [""]
    result: list[PickItem | None] = [None]

    def _visible() -> list[PickItem]:
        if not filter_text[0]:
            return items
        scored = [(fuzzy_score(filter_text[0], it.label), it) for it in items]
        return [it for s, it in sorted(scored, key=lambda t: -t[0]) if s > 0]

    label_width = min(48, max((len(it.label) for it in items), default=20))

    def _render() -> FormattedText:
        visible = _visible()
        if visible:
            selected_index[0] = min(selected_index[0], len(visible) - 1)
        lines: list[tuple[str, str]] = [
            ("bold", f"  {title}  "),
            ("fg:ansibrightblack", "(type to filter · ↑↓ navigate · Enter select · Esc cancel)\n"),
        ]
        if filter_text[0]:
            lines.append(("fg:ansicyan", f"  filter: {filter_text[0]}\n\n"))
        else:
            lines.append(("", "\n"))
        if not visible:
            lines.append(("fg:ansiyellow", "  No matches.\n"))
            return FormattedText(lines)

        # Group headers only make sense in unfiltered order.
        show_groups = not filter_text[0]
        last_group: str | None = None
        for i, item in enumerate(visible):
            if show_groups and item.group and item.group != last_group:
                lines.append(("fg:ansiyellow", f"  — {item.group} —\n"))
                last_group = item.group
            is_sel = i == selected_index[0]
            prefix = "❯ " if is_sel else "  "
            row_style = "bold fg:cyan" if is_sel else ""
            lines.append((row_style, f"  {prefix}{item.label:<{label_width}}"))
            if item.meta:
                lines.append(("fg:ansibrightblack", f"  {item.meta}"))
            if item.is_current:
                lines.append(("fg:ansigreen", " (current)"))
            lines.append(("", "\n"))
        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:
        count = len(_visible())
        if count:
            selected_index[0] = (selected_index[0] - 1) % count

    @kb.add("down")
    def _down(event) -> None:
        count = len(_visible())
        if count:
            selected_index[0] = (selected_index[0] + 1) % count

    @kb.add("enter")
    def _enter(event) -> None:
        visible = _visible()
        if visible:
            result[0] = visible[selected_index[0]]
        event.app.exit()

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit()

    @kb.add("backspace")
    def _backspace(event) -> None:
        filter_text[0] = filter_text[0][:-1]
        selected_index[0] = 0

    @kb.add("<any>")
    def _type(event) -> None:
        ch = event.data
        if ch and ch.isprintable():
            filter_text[0] += ch
            selected_index[0] = 0

    app = Application(
        layout=Layout(Window(content=FormattedTextControl(_render, focusable=True))),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    await app.run_async()
    return result[0]
