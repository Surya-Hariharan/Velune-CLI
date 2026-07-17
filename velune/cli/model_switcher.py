"""Alt+Up / Alt+Down quick model switcher for the interactive REPL.

A terminal has no concept of "key held down" or "key released" — every
keypress (even a repeat while a key is held) arrives as a discrete escape
sequence, and there is no signal when a modifier is lifted. So the natural
"hold Alt, tap arrows to preview, let go to commit" gesture from GUI
switchers (Alt-Tab and friends) is approximated instead: each Alt+Up/Down
press immediately previews the next/previous connected model (so the status
bar reflects it right away) without persisting anything, and a short pause
between presses stands in for "released the key" and commits the pick —
saving it as the default and printing the usual confirmation. Enter commits
early; Esc cancels back to whatever was active before the cycle started.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, Float, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from velune.cli import design

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor

# How long a pause between presses stands in for "the key was released".
_COMMIT_DELAY_SECONDS = 1.1
_VISIBLE_ROWS = 8


@dataclass
class _State:
    visible: bool = False
    models: list[Any] = field(default_factory=list)
    index: int = 0
    original: Any | None = None


class ModelSwitcher:
    """Owns the Alt+Up/Down cycle state and its floating overlay."""

    def __init__(self, repl: Any) -> None:
        self._repl = repl
        self._state = _State()
        self._commit_task: asyncio.Task | None = None

    def is_visible(self) -> bool:
        return self._state.visible

    # -- model source ---------------------------------------------------

    def _connected_models(self) -> list[ModelDescriptor]:
        """Local models plus cloud models whose provider has a working key —
        the same "usable right now" pool /model discover offers."""
        try:
            registry = self._repl.container.get("runtime.model_registry")
            provider_registry = self._repl.container.get("runtime.provider_registry")
            all_models = registry.list_all()
        except Exception:
            return []
        connected = [
            m
            for m in all_models
            if m.is_local or provider_registry.check_provider_available(m.provider_id)
        ]
        connected.sort(key=lambda m: (not m.is_local, m.provider_id, m.model_id))
        return connected

    @staticmethod
    def _same(a: Any, b: Any) -> bool:
        return a is not None and b is not None and a.model_id == b.model_id and a.provider_id == b.provider_id

    # -- interaction ------------------------------------------------------

    def _open(self) -> None:
        st = self._state
        st.models = self._connected_models()
        st.original = self._repl.active_model
        st.index = next(
            (i for i, m in enumerate(st.models) if self._same(m, st.original)),
            0,
        )
        st.visible = bool(st.models)

    def move(self, amount: int) -> None:
        st = self._state
        if not st.visible:
            self._open()
        if not st.models:
            return
        st.index = (st.index + amount) % len(st.models)
        # Live preview only — the status bar reads repl.active_model directly,
        # so this alone makes the switch visible without touching disk.
        self._repl.active_model = st.models[st.index]
        self._schedule_commit()

    def _schedule_commit(self) -> None:
        if self._commit_task is not None and not self._commit_task.done():
            self._commit_task.cancel()

        async def _debounced() -> None:
            try:
                await asyncio.sleep(_COMMIT_DELAY_SECONDS)
            except asyncio.CancelledError:
                return
            self._commit()

        self._commit_task = asyncio.create_task(_debounced())

    def _commit(self) -> None:
        st = self._state
        if not st.visible or not st.models:
            return
        model = st.models[st.index]
        st.visible = False
        from velune.cli.handlers.model import activate_model

        asyncio.create_task(activate_model(self._repl, model))
        self._invalidate()

    def commit_now(self) -> None:
        if self._commit_task is not None and not self._commit_task.done():
            self._commit_task.cancel()
        self._commit()

    def cancel(self) -> None:
        if self._commit_task is not None and not self._commit_task.done():
            self._commit_task.cancel()
        st = self._state
        if st.visible:
            self._repl.active_model = st.original
        st.visible = False

    @staticmethod
    def _invalidate() -> None:
        try:
            get_app().invalidate()
        except Exception:
            pass

    # -- rendering --------------------------------------------------------

    def render(self) -> FormattedText:
        st = self._state
        lines: list[tuple[str, str]] = [
            ("class:model-switcher.label", "  Alt+↑/↓ cycle  ·  Enter keep  ·  Esc cancel\n\n"),
        ]
        if not st.models:
            lines.append(("class:model-switcher.muted", "  No connected models\n"))
            return FormattedText(lines)

        total = len(st.models)
        start = 0
        if total > _VISIBLE_ROWS:
            start = min(max(0, st.index - _VISIBLE_ROWS // 2), total - _VISIBLE_ROWS)
        visible = st.models[start : start + _VISIBLE_ROWS]

        for offset, model in enumerate(visible):
            i = start + offset
            selected = i == st.index
            marker = "›" if selected else " "
            tag = "local" if model.is_local else "cloud"
            style = "class:model-switcher.selected" if selected else "class:model-switcher.command"
            name = model.display_name or model.model_id
            if len(name) > 28:
                name = name[:27].rstrip() + "…"
            lines.append((style, f" {marker} {name:<29}"))
            lines.append(("class:model-switcher.muted", f"{model.provider_id:<11}{tag}\n"))

        end = start + len(visible)
        if start > 0 or end < total:
            lines.append(("class:model-switcher.muted", f"\n  {start + 1}-{end} of {total}\n"))
        return FormattedText(lines)

    # -- wiring -------------------------------------------------------------

    def add_bindings(self, bindings: KeyBindings) -> None:
        visible = Condition(self.is_visible)

        @bindings.add("escape", "up", eager=True)
        def _up(event) -> None:
            self.move(-1)
            event.app.invalidate()

        @bindings.add("escape", "down", eager=True)
        def _down(event) -> None:
            self.move(1)
            event.app.invalidate()

        @bindings.add("enter", filter=visible, eager=True)
        def _confirm(event) -> None:
            self.commit_now()
            event.app.invalidate()

        @bindings.add("escape", filter=visible, eager=True)
        def _cancel(event) -> None:
            self.cancel()
            event.app.invalidate()

    def container(self) -> ConditionalContainer:
        window = Window(
            content=FormattedTextControl(self.render),
            width=Dimension(min=32, preferred=44),
        )
        frame = Frame(
            window,
            title=[("class:model-switcher.frame-title", " MODEL SWITCH ")],
            style="class:model-switcher.frame",
        )
        return ConditionalContainer(frame, filter=Condition(self.is_visible))


MODEL_SWITCHER_STYLES: dict[str, str] = {
    "model-switcher.frame": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "model-switcher.frame-title": f"bg:{design.SURFACE} fg:{design.ACCENT} bold",
    "model-switcher.label": f"bg:{design.SURFACE} fg:{design.MUTED} bold",
    "model-switcher.command": f"bg:{design.SURFACE} fg:{design.WHITE}",
    "model-switcher.selected": f"bg:{design.LIGHT_BG} fg:{design.ACCENT_SOFT} bold",
    "model-switcher.muted": f"bg:{design.SURFACE} fg:{design.FAINT}",
}
