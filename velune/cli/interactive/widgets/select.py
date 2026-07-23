"""Single-choice and multi-choice list widget.

Generalizes the three independent copies of this pattern that existed before
(``picker.py::pick``, ``provider_ui.py::ProviderPalette._show_menu``, and the
REPL's model picker in ``repl.py``): arrow-key navigation, optional
type-to-filter, optional grouping, and — new — checkbox multi-select with
Space to toggle. One implementation now backs single-select menus (mode
selection, model choice, validation retry) and multi-select checklists
(provider configuration) alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from velune.cli import design
from velune.cli.autocomplete import fuzzy_score
from velune.cli.interactive.widget import Widget


@dataclass(slots=True)
class Option:
    """One selectable row. ``meta`` is dim trailing detail; ``badge`` is a
    short status label (e.g. "already configured").

    A badge defaults to success color, which is right for the "you already have
    this" case it was built for — but a badge can also carry bad news ("invalid",
    "offline"), so ``badge_style`` overrides the color when the state isn't good.
    """

    id: str
    label: str
    meta: str = ""
    group: str | None = None
    badge: str | None = None
    badge_style: str | None = None
    disabled: bool = False


# Backwards-compatible alias — picker.py's existing callers construct PickItem.
PickItem = Option


@dataclass(kw_only=True)
class SelectWidget(Widget):
    """Arrow-key list. ``multiple=False`` submits one id; ``multiple=True``
    submits a list of checked ids (Space toggles, Enter confirms the set).
    """

    title: str
    options: list[Option]
    multiple: bool = False
    filterable: bool = False
    subtitle: str = ""
    initial_index: int = 0
    initial_checked: frozenset[str] = field(default_factory=frozenset)

    _index: int = field(default=0, init=False)
    _checked: set[str] = field(default_factory=set, init=False)
    _filter: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._checked = set(self.initial_checked)
        self._index = max(0, min(self.initial_index, len(self.options) - 1))

    # -- visible/filtered options -----------------------------------------

    def _visible(self) -> list[Option]:
        if not self.filterable or not self._filter:
            return [o for o in self.options if not o.disabled]
        q = self._filter.lower()
        scored = [(fuzzy_score(q, opt.label), opt) for opt in self.options if not opt.disabled]
        return [opt for score, opt in sorted(scored, key=lambda t: -t[0]) if score > 0]

    # -- driving the widget --------------------------------------------------
    #
    # Public because two different hosts drive this widget: ``run_standalone``
    # goes through ``key_bindings()`` below, while ``InlineFlow`` binds the
    # REPL's own Application keys straight to these methods (it cannot merge a
    # per-widget KeyBindings into a long-lived Application built at startup).
    # Both paths therefore move the same state through the same entry points.

    def move(self, delta: int) -> None:
        visible = self._visible()
        if not visible:
            return
        self._index = (self._index + delta) % len(visible)

    def type_char(self, char: str) -> bool:
        """Append *char* to the filter. Returns False if it wasn't consumed."""
        if not self.filterable or not char or not char.isprintable():
            return False
        if char == " " and self.multiple:
            return False
        self._filter += char
        self._index = 0
        return True

    def backspace(self) -> None:
        if self.filterable and self._filter:
            self._filter = self._filter[:-1]
            self._index = 0

    def submit(self) -> None:
        self._submit()

    def toggle_current(self) -> None:
        self._toggle_current()

    def _toggle_current(self) -> None:
        visible = self._visible()
        if not visible:
            return
        opt = visible[self._index % len(visible)]
        if opt.id in self._checked:
            self._checked.discard(opt.id)
        else:
            self._checked.add(opt.id)

    def _submit(self) -> None:
        if self.multiple:
            ordered = [o.id for o in self.options if o.id in self._checked]
            self.on_submit(ordered)
            return
        visible = self._visible()
        if visible:
            self.on_submit(visible[self._index % len(visible)].id)

    # -- rendering -----------------------------------------------------------

    def render(self) -> StyleAndTextTuples:
        visible = self._visible()
        if visible:
            self._index = min(self._index, len(visible) - 1)

        lines: StyleAndTextTuples = []
        lines.append((f"bold fg:{design.ACCENT}", f"  {self.title}\n"))
        if self.subtitle:
            lines.append((f"fg:{design.MUTED}", f"  {self.subtitle}\n"))
        lines.append(("", "\n"))

        if self.filterable and self._filter:
            lines.append((f"fg:{design.INFO}", f"  filter: {self._filter}\n\n"))

        if not visible:
            lines.append((f"fg:{design.WARN}", "  No matches.\n"))
            return lines

        show_groups = self.filterable and not self._filter
        last_group: str | None = None
        for i, opt in enumerate(visible):
            if show_groups and opt.group and opt.group != last_group:
                lines.append((f"fg:{design.FAINT}", f"  — {opt.group} —\n"))
                last_group = opt.group

            is_sel = i == self._index
            prefix = "❯ " if is_sel else "  "
            row_style = f"bold fg:{design.ACCENT}" if is_sel else f"fg:{design.WHITE}"

            if self.multiple:
                box = "[x]" if opt.id in self._checked else "[ ]"
                box_style = (
                    f"bold fg:{design.OK}" if opt.id in self._checked else f"fg:{design.FAINT}"
                )
                lines.append((row_style, f"  {prefix}"))
                lines.append((box_style, box))
                lines.append((row_style, f" {opt.label}"))
            else:
                lines.append((row_style, f"  {prefix}{opt.label}"))

            if opt.meta:
                lines.append((f"fg:{design.MUTED}", f"  {opt.meta}"))
            if opt.badge:
                lines.append((f"fg:{opt.badge_style or design.OK}", f"  {opt.badge}"))
            lines.append(("", "\n"))

        return lines

    # -- key bindings ----------------------------------------------------

    def key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("s-tab")
        def _up(event) -> None:
            self.move(-1)

        @kb.add("down")
        @kb.add("tab")
        def _down(event) -> None:
            self.move(1)

        # Mouse wheel — both hosts (runner.run_standalone, chrome.WizardController)
        # already enable mouse_support=True, but nothing consumed it: scrolling
        # over any select list silently did nothing. Wired here once so every
        # caller (single_select, multi_select, model/provider pickers, tool
        # approval) gets it for free.
        @kb.add(Keys.ScrollUp, eager=True)
        def _scroll_up(event) -> None:
            self.move(-1)

        @kb.add(Keys.ScrollDown, eager=True)
        def _scroll_down(event) -> None:
            self.move(1)

        @kb.add("enter")
        def _enter(event) -> None:
            self.submit()

        if self.multiple:

            @kb.add(" ")
            def _toggle(event) -> None:
                self.toggle_current()

        if self.filterable:

            @kb.add("backspace")
            def _bs(event) -> None:
                self.backspace()

            @kb.add("<any>")
            def _type(event) -> None:
                self.type_char(event.data)

        return kb

    def footer_hint(self) -> str:
        parts = ["↑↓ navigate"]
        if self.multiple:
            parts.append("Space toggle")
        parts.append("Enter " + ("continue" if self.multiple else "select"))
        parts.append("Esc back")
        if self.filterable:
            parts.append("type to filter")
        return "  ·  ".join(parts)
