"""Floating theme palette for the interactive REPL, opened by typing ``/theme``.

The third sibling of :mod:`velune.cli.command_palette` and
:mod:`velune.cli.model_palette`: same float geometry, same two-pane frame, same
key contract (``↑``/``↓`` navigate, ``Enter`` select, ``Esc`` close). Nothing
about the interaction model is re-invented here.

It differs from those two in one respect, and deliberately. The command and
model palettes *submit a command* on Enter — they set the buffer to
``/model use <id>`` and let the normal command path do the work. This one
applies the theme directly, because there is no separate command to route
through: the theme registry and the live restyle are the whole operation, and
routing through a synthetic ``/theme use <id>`` submission would put a line in
the transcript for what is a pure display preference.

Consequences of applying in place, both required by the brief:

* ``/theme`` never reaches inference. Enter is bound with ``eager=True`` under
  an ``is_active`` filter, so the keypress is consumed by this palette and the
  buffer is cleared rather than validated-and-handled. No prompt is submitted,
  so no inference request is created.
* The prompt editor is left clean. The literal text ``/theme`` is erased from
  the buffer as part of selection.
"""

from __future__ import annotations

from collections.abc import Callable
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

from velune.cli import design, themes

if TYPE_CHECKING:
    from prompt_toolkit.shortcuts import PromptSession

#: Command head that opens this palette.
_TRIGGER = "/theme"

#: ``/theme`` still carries the accessibility sub-commands (``colorblind``,
#: ``motion``) that predate colour themes — they are independent display
#: modifiers, not themes, and Part 2D of this rework keeps them. When the first
#: word after ``/theme`` is one of these the user is driving a sub-command, so
#: the palette must stay shut and Enter must submit the command normally.
#: ``status`` is included for the same reason.
_SUBCOMMANDS = frozenset(
    {
        "colorblind",
        "cb",
        "motion",
        "animation",
        "animations",
        "status",
    }
)


class ThemePaletteModel:
    """Filtering and keyboard selection state, independent of any terminal."""

    def __init__(self, themes_source: Callable[[], list[themes.Theme]] | None = None) -> None:
        self._themes_source = themes_source or themes.list_themes
        self.selected_index = 0
        self._last_query: str | None = None

    @staticmethod
    def query_from_text(text: str) -> str | None:
        """The filter query for *text*, or None if the palette should stay shut."""
        stripped = text.lstrip()
        if not stripped.startswith(_TRIGGER):
            return None
        rest = stripped[len(_TRIGGER) :]
        # "/themes" is a different word, not "/theme" + a filter.
        if rest and not rest[0].isspace():
            return None
        query = rest.strip()
        if query and query.split()[0].lower() in _SUBCOMMANDS:
            return None
        return query

    def matches(self, query: str) -> list[themes.Theme]:
        if query != self._last_query:
            self.selected_index = 0
            self._last_query = query

        needle = query.strip().lower()
        results = [
            theme
            for theme in self._themes_source()
            if not needle or needle in theme.id.lower() or needle in theme.name.lower()
        ]
        if results:
            self.selected_index = min(self.selected_index, len(results) - 1)
        else:
            self.selected_index = 0
        return results

    def move(self, query: str, amount: int) -> None:
        matches = self.matches(query)
        if matches:
            self.selected_index = (self.selected_index + amount) % len(matches)

    def selected(self, query: str) -> themes.Theme | None:
        matches = self.matches(query)
        if not matches:
            return None
        return matches[self.selected_index]


class ThemePalette:
    """prompt_toolkit renderer and key bindings for :class:`ThemePaletteModel`."""

    def __init__(
        self,
        *,
        suppressed: Callable[[], bool] | None = None,
        on_select: Callable[[themes.Theme], None] | None = None,
        themes_source: Callable[[], list[themes.Theme]] | None = None,
    ) -> None:
        # "Something else owns the prompt box" — e.g. an InlineFlow step whose
        # filter text could itself look like a /theme invocation.
        self._suppressed = suppressed
        #: Called with the chosen theme. The REPL passes a callback that
        #: persists it and rebuilds the live style; this module never writes
        #: config or touches the Application itself.
        self._on_select = on_select
        self.model = ThemePaletteModel(themes_source)
        self._dismissed_text: str | None = None

    # -- state ------------------------------------------------------------

    def _buffer_text(self) -> str:
        try:
            return get_app().current_buffer.text
        except Exception:
            return ""

    def query(self) -> str | None:
        return self.model.query_from_text(self._buffer_text())

    def is_active(self) -> bool:
        if self._suppressed is not None and self._suppressed():
            return False
        text = self._buffer_text()
        return self.model.query_from_text(text) is not None and text != self._dismissed_text

    def dismiss(self) -> None:
        self._dismissed_text = self._buffer_text()

    def _selected(self) -> themes.Theme | None:
        query = self.query()
        return self.model.selected(query) if query is not None else None

    # -- rendering --------------------------------------------------------

    def render_themes(self) -> FormattedText:
        query = self.query() or ""
        matches = self.model.matches(query)
        count = len(matches)
        active_id = design.active_theme_id()

        lines: list[tuple[str, str]] = [
            ("class:theme-palette.label", "  THEME\n"),
            ("class:theme-palette.query", f"  {_TRIGGER} {query}".rstrip()),
            ("class:theme-palette.muted", f"  {count} theme{'s' if count != 1 else ''}\n\n"),
        ]

        if not matches:
            lines.append(("class:theme-palette.warning", f"  No theme matches “{query}”\n"))
            return FormattedText(lines)

        for index, theme in enumerate(matches):
            selected = index == self.model.selected_index
            marker = ">" if selected else " "
            style = "class:theme-palette.selected" if selected else "class:theme-palette.theme"
            # A dot rendered in the theme's own primary hue, so the list is a
            # swatch strip rather than a set of names you have to try one by
            # one to see. Inline styles are the only raw colours in this
            # module and they come straight from the registry.
            lines.append((f"bg:{design.SURFACE} fg:{theme.primary}", f" {marker} ●"))
            lines.append((style, f" {theme.name:<16}"))
            lines.append(
                (
                    "class:theme-palette.active"
                    if theme.id == active_id
                    else "class:theme-palette.muted",
                    f"{'active' if theme.id == active_id else ''}\n",
                )
            )
        return FormattedText(lines)

    def render_details(self) -> FormattedText:
        theme = self._selected()
        if theme is None:
            return FormattedText([("class:theme-palette.muted", "  Use ↑/↓ to choose a theme.")])

        lines: list[tuple[str, str]] = [
            ("class:theme-palette.title", f"  {theme.name}\n"),
            ("class:theme-palette.muted", f"  {theme.description}\n\n"),
            ("class:theme-palette.label", "  PALETTE\n"),
        ]

        # Live swatches in the candidate theme's own colours — the point of a
        # preview is to show the colours, so these are intentionally painted
        # from the theme under the cursor rather than the active one.
        swatches: tuple[tuple[str, str], ...] = (
            ("primary", theme.primary),
            ("accent", theme.accent),
            ("success", theme.success),
            ("warning", theme.warning),
            ("error", theme.error),
        )
        for label, color in swatches:
            lines.append((f"bg:{design.SURFACE} fg:{color}", "  ████ "))
            lines.append(("class:theme-palette.text", f"{label}\n"))

        lines.append(("class:theme-palette.muted", "\n  ↑/↓ navigate   Enter apply   Esc close"))
        return FormattedText(lines)

    # -- wiring -----------------------------------------------------------

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

        @bindings.add("enter", filter=active, eager=True)
        def _select(event) -> None:
            theme = self._selected()
            # Whatever happens, Enter is consumed here: this binding never
            # calls validate_and_handle(), so the "/theme" text is never
            # submitted as a prompt and no inference request is created.
            event.current_buffer.text = ""
            event.current_buffer.cursor_position = 0
            self._dismissed_text = None
            if theme is not None and self._on_select is not None:
                self._on_select(theme)
            event.app.invalidate()

        @bindings.add("escape", filter=active, eager=True)
        def _close(event) -> None:
            self.dismiss()
            event.app.invalidate()

    def container(self) -> ConditionalContainer:
        left = Window(
            content=FormattedTextControl(self.render_themes),
            width=Dimension(min=25, preferred=34),
            dont_extend_width=False,
        )
        divider = Window(width=1, char="|", style="class:theme-palette.border")
        right = Window(
            content=FormattedTextControl(self.render_details),
            width=Dimension(min=30, weight=2),
        )
        body = VSplit([left, divider, right], padding=1, padding_style="class:theme-palette.border")
        frame = Frame(
            body,
            title=[("class:theme-palette.frame-title", " THEME PALETTE ")],
            style="class:theme-palette.frame",
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


def theme_palette_styles() -> dict[str, str]:
    """Theme-palette style rules for the *currently active* theme.

    A function rather than a module-level dict so a theme change is picked up
    — see ``design.apply_theme``. This palette needs it more than its siblings
    do: it is the surface that *causes* the change, and it stays open across
    the swap, so a frozen dict would leave it painted in the outgoing theme
    while everything behind it had already moved to the new one.
    """
    return {
        "theme-palette.frame": f"bg:{design.SURFACE} fg:{design.FAINT}",
        "theme-palette.frame-title": f"bg:{design.SURFACE} fg:{design.ACCENT} bold",
        "theme-palette.border": f"bg:{design.SURFACE} fg:{design.FAINT}",
        "theme-palette.title": f"bg:{design.SURFACE} fg:{design.WHITE} bold",
        "theme-palette.label": f"bg:{design.SURFACE} fg:{design.MUTED} bold",
        "theme-palette.query": f"bg:{design.SURFACE} fg:{design.ACCENT} bold",
        "theme-palette.theme": f"bg:{design.SURFACE} fg:{design.WHITE}",
        "theme-palette.selected": f"bg:{design.LIGHT_BG} fg:{design.ACCENT} bold",
        "theme-palette.active": f"bg:{design.SURFACE} fg:{design.OK}",
        "theme-palette.text": f"bg:{design.SURFACE} fg:{design.WHITE}",
        "theme-palette.muted": f"bg:{design.SURFACE} fg:{design.FAINT}",
        "theme-palette.warning": f"bg:{design.SURFACE} fg:{design.WARN}",
    }


def all_palette_styles() -> dict[str, str]:
    """Every theme-dependent style rule in the CLI, for the active theme.

    The single place the fullscreen UI's ``Style`` is assembled from, so a
    theme swap rebuilds *all* surfaces at once and none can be forgotten. Any
    new palette adds its builder here rather than to a call site.
    """
    from velune.cli.command_palette import palette_styles
    from velune.cli.home import home_styles
    from velune.cli.model_palette import model_palette_styles
    from velune.cli.model_switcher import model_switcher_styles
    from velune.cli.statusbar import status_bar_styles

    return {
        "prompt.arrow": f"{design.ACCENT_SOFT} bold",
        **status_bar_styles(),
        **palette_styles(),
        **model_palette_styles(),
        **model_switcher_styles(),
        **theme_palette_styles(),
        **home_styles(),
    }
