"""Persistent full-screen wizard chrome: header + sidebar + body + footer.

One long-lived ``Application(full_screen=True)`` backs the entire onboarding
(or setup) run. Moving between stages — and between sub-steps within a
stage, e.g. mode-select -> provider-checklist -> key-entry -> validation —
swaps which widget is embedded in the body via ``DynamicContainer`` /
``DynamicKeyBindings`` and calls ``invalidate()``. There is no repeated
alt-screen enter/exit and no flicker, while the sidebar/header still
re-render on every stage change so it reads as "one screen per stage".

Esc and Ctrl-C are deliberately different scopes: Esc ("back") is per-widget
— each ``run_widget()`` call wires it to that step's own outcome so the
stage-sequencing code decides what "back" means. Ctrl-C ("cancel") is
wizard-global — it always aborts the whole run via ``KeyboardInterrupt``,
matching the pre-existing ``run_onboarding()`` contract, regardless of which
widget currently has focus.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import merge_key_bindings
from prompt_toolkit.key_binding.key_bindings import DynamicKeyBindings, KeyBindingsBase
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D

from velune.cli import design
from velune.cli.interactive.keys import common_bindings
from velune.cli.interactive.result import BACK
from velune.cli.interactive.widget import Widget
from velune.cli.interactive.widgets.text import TextInputWidget

# Sidebar collapses below this terminal width — matches fullscreen.py's
# existing narrow-terminal threshold pattern.
SIDEBAR_MIN_COLUMNS = 70

T = TypeVar("T")


@dataclass(slots=True)
class StageInfo:
    key: str
    title: str


class WizardCancelled(Exception):
    """Raised out of ``WizardController.run()`` when Ctrl-C is pressed."""


class WizardController:
    """Owns the single full-screen ``Application`` for a wizard run."""

    def __init__(
        self,
        brand: str,
        stages: list[StageInfo],
        *,
        input: Any | None = None,
        output: Any | None = None,
    ) -> None:
        self.brand = brand
        self.stages = stages
        self.completed: set[str] = set()
        self.current_index: int = 0
        self.status_line: str = ""

        self._widget: Widget | TextInputWidget | None = None
        self._transient_frame: StyleAndTextTuples | None = None
        self._app: Application | None = None
        self._cancel_event = asyncio.Event()
        # None keeps prompt_toolkit's own defaults; tests inject a pipe input
        # + DummyOutput to drive real key events without a real terminal.
        self._input = input
        self._output = output

    # -- public: called from stage-authoring code --------------------------

    def mark_complete(self, stage_key: str) -> None:
        self.completed.add(stage_key)

    def set_status(self, text: str) -> None:
        self.status_line = text

    def request_cancel(self) -> None:
        """Abort the whole wizard, as if the user pressed Ctrl-C.

        For stage code that offers its own "quit setup" menu option distinct
        from the always-available Ctrl-C shortcut.
        """
        self._cancel_event.set()

    async def run(self, body: Callable[[], Awaitable[T]]) -> T:
        """Start the persistent ``Application`` and drive *body* alongside it.

        *body* is the stage-sequencing coroutine — it calls ``run_widget()``
        / ``show_transient()`` / ``mark_complete()`` as it walks the stage
        list. The Application's own ``run_async()`` is what actually reads
        key input and renders; the other methods only swap state and call
        ``invalidate()``. Raises ``WizardCancelled`` if Ctrl-C is pressed at
        any point — callers should treat that the same as the old
        ``KeyboardInterrupt`` contract.
        """
        app = self._ensure_app()
        app_task = asyncio.ensure_future(app.run_async())
        body_task = asyncio.ensure_future(body())
        cancel_task = asyncio.ensure_future(self._cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {body_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                body_task.cancel()
                raise WizardCancelled()
            return body_task.result()
        finally:
            cancel_task.cancel()
            if app.is_running:
                app.exit()
            await app_task

    async def run_widget(self, widget: Widget | TextInputWidget, *, stage_index: int) -> Any:
        """Embed *widget* as the active body; await and return its outcome.

        Returns whatever the widget submits, or ``BACK`` if Esc was pressed.
        Ctrl-C never resolves here — it's handled globally by ``run()``.
        """
        self.current_index = stage_index
        loop = asyncio.get_event_loop()
        result_future: asyncio.Future = loop.create_future()

        # Note: two keypresses delivered in the exact same input read (never
        # observed from real human typing or a real terminal driver, only
        # possible from synthetic/piped input sent as one unbuffered batch)
        # can resolve against this widget before the coroutine below has a
        # chance to install the *next* one. The stale widget's handler still
        # fires but is a no-op once `result_future` is done, so the extra
        # keypress is silently absorbed rather than corrupting state — worst
        # case the user presses the key once more.
        def _resolve(value: Any) -> None:
            if not result_future.done():
                result_future.set_result(value)

        widget.on_submit = _resolve
        widget.on_back = lambda: _resolve(BACK)
        self._widget = widget

        app = self._ensure_app()
        app.invalidate()
        try:
            return await result_future
        finally:
            self._widget = None

    async def show_transient(
        self,
        frames: list[StyleAndTextTuples],
        *,
        delay: float = 0.35,
        final_delay: float | None = None,
    ) -> None:
        """Render a sequence of non-interactive frames, pausing between each.

        Used for animated checklists ("✓ CPU / ✓ RAM / ✓ GPU / ...") and
        brief transition flashes ("Validating... / ✓ Connected / Loading
        next step..."). Ctrl-C still works during a transient frame (handled
        globally by ``run()``); there is nothing else to interact with.
        """
        if not frames:
            return
        app = self._ensure_app()
        prev_widget = self._widget
        self._widget = None
        try:
            for i, frame in enumerate(frames):
                self._transient_frame = frame
                app.invalidate()
                is_last = i == len(frames) - 1
                await asyncio.sleep(final_delay if (is_last and final_delay is not None) else delay)
        finally:
            self._transient_frame = None
            self._widget = prev_widget
            app.invalidate()

    async def close(self) -> None:
        if self._app is not None and self._app.is_running:
            self._app.exit()

    # -- internal: build (once) the persistent Application ------------------

    def _ensure_app(self) -> Application:
        if self._app is not None:
            return self._app

        def _get_container():
            if self._transient_frame is not None:
                frame = self._transient_frame
                return Window(FormattedTextControl(lambda: frame))
            if self._widget is None:
                return Window(FormattedTextControl(lambda: []))
            if isinstance(self._widget, TextInputWidget):
                return self._widget.container
            return Window(
                FormattedTextControl(self._widget.render, focusable=True),
                wrap_lines=True,
            )

        def _get_key_bindings() -> KeyBindingsBase:
            if self._widget is None:
                return common_bindings(on_cancel=self._cancel_event.set, on_back=None)
            common = common_bindings(on_cancel=self._cancel_event.set, on_back=self._widget.on_back)
            if isinstance(self._widget, TextInputWidget):
                return common
            return merge_key_bindings([self._widget.key_bindings(), common])

        body = DynamicContainer(_get_container)
        body_kb = DynamicKeyBindings(_get_key_bindings)

        header_window = Window(
            FormattedTextControl(self._render_header),
            height=D.exact(4),
            dont_extend_height=True,
        )
        sidebar_window = Window(
            FormattedTextControl(self._render_sidebar),
            width=D.exact(22),
            dont_extend_width=True,
        )
        footer_window = Window(
            FormattedTextControl(self._render_footer),
            height=D.exact(1),
            dont_extend_height=True,
        )

        wide_enough = Condition(
            lambda: (
                (self._app.output.get_size().columns if self._app else 999) >= SIDEBAR_MIN_COLUMNS
            )
        )

        main_row = VSplit(
            [
                ConditionalContainer(sidebar_window, filter=wide_enough),
                ConditionalContainer(
                    Window(width=D.exact(1), char="│", style=f"fg:{design.FAINT}"),
                    filter=wide_enough,
                ),
                body,
            ]
        )

        root = HSplit([header_window, main_row, footer_window])

        self._app = Application(
            layout=Layout(root),
            key_bindings=body_kb,
            full_screen=True,
            mouse_support=True,
            terminal_size_polling_interval=0.5,
            input=self._input,
            output=self._output,
        )
        return self._app

    # -- chrome rendering ----------------------------------------------------

    def _render_header(self) -> StyleAndTextTuples:
        total = len(self.stages)
        idx = self.current_index + 1
        title = self.stages[self.current_index].title if self.stages else ""
        rule = "━" * 60
        return [
            (f"fg:{design.FAINT}", f"  {rule}\n"),
            (f"bold fg:{design.ACCENT}", f"  {self.brand}\n"),
            (f"fg:{design.MUTED}", f"  Step {idx} / {total}    "),
            (f"bold fg:{design.WHITE}", f"{title}\n"),
            (f"fg:{design.FAINT}", f"  {rule}"),
        ]

    def _render_sidebar(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = [(f"bold fg:{design.WHITE}", "\n  Setup\n\n")]
        for i, stage in enumerate(self.stages):
            if stage.key in self.completed:
                marker, style = "✓", f"fg:{design.OK}"
            elif i == self.current_index:
                marker, style = "❯", f"bold fg:{design.ACCENT}"
            else:
                marker, style = "○", f"fg:{design.FAINT}"
            label_style = (
                f"bold fg:{design.WHITE}" if i == self.current_index else f"fg:{design.MUTED}"
            )
            lines.append((style, f"  {marker} "))
            lines.append((label_style, f"{stage.title}\n"))
        return lines

    def _render_footer(self) -> StyleAndTextTuples:
        if self._transient_frame is not None:
            hint = ""
        else:
            hint = self._widget.footer_hint() if self._widget is not None else ""
        extra = f"    {self.status_line}" if self.status_line else ""
        return [(f"fg:{design.FAINT}", f"  {hint}{extra}")]
