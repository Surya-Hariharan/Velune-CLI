"""Command-palette-styled variant of :class:`SelectWidget`.

Same selection model, same keys, same result contract — only the presentation
differs: a framed two-column panel where the left column is a searchable,
grouped result list and the right column describes whatever row is highlighted.
It is the palette users already know from typing ``/`` in the REPL, reused for
"pick a provider to connect", so choosing a thing looks the same everywhere in
the app.

Rendering lives here rather than in ``select.py`` because the plain list form is
still the right shape inside the onboarding wizard's sidebar chrome, where a
second nested frame would fight the frame already on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import AnyContainer

from velune.cli.interactive import panel
from velune.cli.interactive.widgets.select import Option, SelectWidget

# Rows shown at once before the list scrolls. Matches the REPL palette, and
# bounds panel height in a non-full-screen Application, where the panel is
# drawn inline and would otherwise grow with the option count.
_VISIBLE_ROWS = 9

# Label is clipped to _LABEL_WIDTH and padded one wider, so a full-width label
# still has a gap before its description instead of running into it. Meta is
# clipped to what actually survives the left pane's width; the detail pane
# carries the untruncated text.
_LABEL_WIDTH = 16
_META_WIDTH = 13


@dataclass(kw_only=True)
class PaletteSelectWidget(SelectWidget):
    """A :class:`SelectWidget` drawn as the two-pane command palette."""

    # Frame caption. Defaults to the widget title, uppercased by ``framed``.
    frame_title: str = ""

    # The detail pane ends with footer_hint(), so the host must not add its own
    # hint strip below the frame (see runner.run_standalone).
    shows_own_hints: bool = True

    _container: AnyContainer | None = field(default=None, init=False, repr=False)

    # -- selection ---------------------------------------------------------

    def _current(self) -> Option | None:
        visible = self._visible()
        if not visible:
            return None
        return visible[self._index % len(visible)]

    # -- panes -------------------------------------------------------------

    def render_results(self) -> StyleAndTextTuples:
        visible = self._visible()
        if visible:
            self._index = min(self._index, len(visible) - 1)
        count = len(visible)

        # A non-filterable widget has no query line — labelling a fixed menu
        # "SEARCH" and showing an empty query invites typing that does nothing.
        if self.filterable:
            lines: StyleAndTextTuples = [(panel.LABEL, "  SEARCH\n")]
            if self._filter:
                lines.append((panel.QUERY, f"  {self._filter}"))
            else:
                lines.append((panel.MUTED, "  type to filter"))
            lines.append((panel.MUTED, f"  {count} result{'s' if count != 1 else ''}\n\n"))
        else:
            lines = [(panel.LABEL, "  OPTIONS\n\n")]
        if not visible:
            lines.append((panel.WARNING, "  No matches\n"))
            return lines

        start = panel.window_start(count, self._index, _VISIBLE_ROWS)
        window = visible[start : start + _VISIBLE_ROWS]
        # Groups are headings over an unfiltered list; once the user is typing,
        # results are ranked by score and group order no longer holds.
        show_groups = not self._filter
        previous_group: str | None = None

        for offset, opt in enumerate(window):
            index = start + offset
            if show_groups and opt.group and opt.group != previous_group:
                if previous_group is not None:
                    lines.append((panel.MUTED, "\n"))
                lines.append((panel.GROUP, f"  {opt.group.upper()}\n"))
                previous_group = opt.group

            selected = index == self._index
            marker = ">" if selected else " "
            style = panel.SELECTED if selected else panel.ROW

            label = f"{_clip(opt.label, _LABEL_WIDTH):<{_LABEL_WIDTH + 1}}"
            if self.multiple:
                box = "[x]" if opt.id in self._checked else "[ ]"
                lines.append((style, f" {marker} {box} {label}"))
            else:
                lines.append((style, f" {marker}  {label}"))
            lines.append((panel.MUTED, f"{_clip(opt.meta, _META_WIDTH)}\n"))

        return lines

    def render_details(self) -> StyleAndTextTuples:
        opt = self._current()
        if opt is None:
            return [(panel.MUTED, "  Nothing matches that search.")]

        lines: StyleAndTextTuples = [
            (panel.TITLE, f"  {opt.label}\n"),
            (panel.MUTED, f"  {opt.group or self.title}\n\n"),
        ]
        if opt.meta:
            lines.append((panel.LABEL, "  DESCRIPTION\n"))
            lines.append((panel.TEXT, f"  {opt.meta}\n\n"))
        if opt.badge:
            lines.append((panel.LABEL, "  STATUS\n"))
            lines.append((panel.CODE, f"  {opt.badge}\n\n"))
        if self.subtitle:
            lines.append((panel.MUTED, f"  {self.subtitle}\n\n"))
        lines.append((panel.MUTED, f"  {self.footer_hint()}"))
        return lines

    # A host that doesn't understand ``container`` (the wizard chrome) still
    # gets a usable single-column list rather than a blank body.
    def render(self) -> StyleAndTextTuples:
        return self.render_results()

    @property
    def container(self) -> AnyContainer:
        if self._container is None:
            self._container = panel.framed(
                panel.two_pane(self.render_results, self.render_details),
                self.frame_title or self.title,
            )
        return self._container

    def footer_hint(self) -> str:
        parts = ["↑↓ navigate"]
        if self.multiple:
            parts.append("Space toggle")
        parts.append("Enter " + ("continue" if self.multiple else "select"))
        parts.append("Esc back")
        if self.filterable:
            parts.append("type to filter")
        return "  ·  ".join(parts)


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"
