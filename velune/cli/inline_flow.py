"""Multi-step interactive flows hosted *inside* the running REPL Application.

``/connect`` used to run each of its steps — pick a provider, paste the key,
watch it verify — as its own throwaway ``prompt_toolkit`` ``Application`` via
``interactive.runner.run_standalone``. That works standalone (``velune setup``),
but inside the REPL it means a second Application drawing itself inline at the
cursor, i.e. *below* the prompt box, while the REPL's own layout is still on
screen above it. Each step tore that panel down and built another one, so a
single flow visibly hopped around the terminal.

This module keeps the whole flow in the REPL's *existing* Application instead:
one float, in exactly the geometry and chrome the ``/`` command palette already
uses (``command_palette.CommandPalette``), sitting above the prompt box; and the
REPL's own prompt buffer as the one and only input surface. Typing filters the
provider list, and after a provider is chosen the very same box — masked —
takes the API key. Nothing moves between steps; only the panel's contents
change.

The stages are deliberately thin wrappers over the same widgets
``run_standalone`` hosts (``PaletteSelectWidget`` above all), so the two hosts
render identically and there is no second copy of the palette's look. What a
stage adds is the mapping from "the prompt buffer's text" to "what this step
means by it".

Awaiting works because the REPL's dispatch loop and the Application's key
handlers are separate tasks over one event loop: ``await flow.select(...)``
parks the dispatch task on a Future that a key binding resolves later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer

from velune.cli import design
from velune.cli.interactive import panel
from velune.cli.interactive.result import BACK, CANCEL, WidgetResult
from velune.cli.interactive.widgets.palette_select import PaletteSelectWidget
from velune.cli.interactive.widgets.select import Option
from velune.cli.interactive.widgets.status import (
    HOLD_SECONDS,
    SPINNER_FRAMES,
    TICK_SECONDS,
)

T = TypeVar("T")

# Returned by ``_Stage.submit()`` to mean "not done — stay open and re-render".
# A plain ``None`` cannot serve here: a stage may legitimately resolve with it.
KEEP = object()


class FlowCancelled(Exception):
    """Raised into the awaiting command when the user aborts a flow with Ctrl+C.

    Esc and Ctrl+C mean genuinely different things here, so they do not share a
    channel. Esc is *step*-level — it resolves the step with ``BACK``/``CANCEL``
    and the caller decides what that means, which for a menu is "go up a level".
    Ctrl+C abandons the whole command, from whichever step is on screen, and the
    caller should not get a chance to interpret it as anything smaller.

    Deliberately not ``asyncio.CancelledError``. That would unwind through the
    REPL's main loop, which re-raises any cancellation the interrupt controller
    did not itself flag as user-initiated — tearing down the REPL, which is the
    precise opposite of the soft cancel this represents. A distinct exception
    also means an intervening ``except Exception`` cannot silently swallow the
    abort and carry on to the next step.
    """


class _Stage:
    """One step: what the panel shows, and what the prompt box's text means."""

    frame_title: str = ""
    # True while the prompt box is the user's text field for this step rather
    # than a navigation filter — drives masking and the prompt caret label.
    entry: bool = False
    masked: bool = False

    def initial_text(self) -> str:
        """Text the prompt box is seeded with when this step opens."""
        return ""

    def sync(self, text: str) -> None:
        """Called before every render with the live prompt-buffer text."""

    def submit(self, text: str) -> Any:
        return KEEP

    def move(self, delta: int) -> None:
        """Arrow-key navigation, where the step has any."""

    def results(self) -> StyleAndTextTuples:
        raise NotImplementedError

    def details(self) -> StyleAndTextTuples:
        raise NotImplementedError

    def prompt_label(self) -> str:
        """Caret label rendered inside the prompt box for this step."""
        return ""

    def hint(self) -> str:
        raise NotImplementedError


class _SelectStage(_Stage):
    """Pick one row. The prompt buffer is the palette's search query."""

    def __init__(self, widget: PaletteSelectWidget) -> None:
        self._widget = widget
        self.frame_title = widget.frame_title or widget.title
        # Seeded with the widget's starting filter, not None: sync() resets the
        # highlight whenever the query changes, so treating the very first sync
        # (an empty box matching an empty filter) as a change would discard
        # `initial_index` and undo any arrow-key movement made before it.
        self._synced: str = widget._filter

    def sync(self, text: str) -> None:
        # The buffer owns the query text (so the user gets real line editing,
        # word-delete and paste for free); the widget only mirrors it. Resetting
        # the highlight on change matches what SelectWidget.type_char does.
        if not self._widget.filterable or text == self._synced:
            return
        self._synced = text
        self._widget._filter = text
        self._widget._index = 0

    def submit(self, text: str) -> Any:
        self.sync(text)
        chosen = self._widget._current()
        return KEEP if chosen is None else chosen.id

    def move(self, delta: int) -> None:
        self._widget.move(delta)

    def results(self) -> StyleAndTextTuples:
        return self._widget.render_results()

    def details(self) -> StyleAndTextTuples:
        return self._widget.render_details()

    def hint(self) -> str:
        parts = ["↑↓ navigate", "Enter select", "Esc cancel"]
        if self._widget.filterable:
            parts.insert(0, "type to filter")
        return "  ·  ".join(parts)


class _TextStage(_Stage):
    """Free text (an API key, above all), typed into the prompt box itself."""

    entry = True

    def __init__(
        self,
        title: str,
        *,
        hint: str = "",
        password: bool = False,
        default: str = "",
        optional: bool = False,
        validate: Callable[[str], str | None] | None = None,
        frame_title: str = "",
    ) -> None:
        self.title = title
        self.frame_title = frame_title or title
        self.masked = password
        self._hint = hint
        self._default = default
        self._optional = optional
        self._validate = validate
        self._error = ""

    def initial_text(self) -> str:
        return self._default

    def sync(self, text: str) -> None:
        # A stale "that key contains spaces" banner sitting over a field the
        # user has since corrected is worse than no banner at all.
        if self._error and text:
            self._error = ""

    def submit(self, text: str) -> Any:
        value = text.strip()
        if not value and self._optional:
            return ""
        if self._validate is not None:
            error = self._validate(value)
            if error:
                self._error = error
                return KEEP
        self._error = ""
        return value

    def results(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = [
            (panel.LABEL, "  STEP\n"),
            (panel.TEXT, f"  {self.title}\n\n"),
        ]
        if self._error:
            lines.append((panel.LABEL, "  PROBLEM\n"))
            lines.append((f"bg:{design.SURFACE} fg:{design.DANGER}", f"  {self._error}\n\n"))
        lines.append((panel.MUTED, "  Type below and press Enter.\n"))
        if self.masked:
            lines.append((panel.MUTED, "  Input is hidden as you type.\n"))
        return lines

    def details(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = [(panel.TITLE, f"  {self.title}\n\n")]
        if self._hint:
            lines.append((panel.LABEL, "  DETAILS\n"))
            lines.append((panel.TEXT, f"  {self._hint}\n\n"))
        lines.append((panel.MUTED, f"  {self.hint()}"))
        return lines

    def prompt_label(self) -> str:
        return self.title

    def hint(self) -> str:
        skip = "  ·  Enter to skip" if self._optional else ""
        return f"Enter confirm{skip}  ·  Esc cancel"


class _StatusStage(_Stage):
    """Spinner → ✓/✗, for the awaited work between two input steps."""

    def __init__(self, pending: str, frame_title: str = "") -> None:
        self.frame_title = frame_title or "Working"
        self._pending = pending
        self._tick = 0
        self._ok: bool | None = None
        self._final = ""

    def advance(self) -> None:
        self._tick += 1

    def settle(self, *, ok: bool, message: str) -> None:
        self._ok = ok
        self._final = message

    def results(self) -> StyleAndTextTuples:
        if self._ok is None:
            frame = SPINNER_FRAMES[self._tick % len(SPINNER_FRAMES)]
            return [
                (panel.LABEL, "  IN PROGRESS\n\n"),
                (f"bg:{design.SURFACE} fg:{design.ACCENT}", f"  {frame} "),
                (panel.TEXT, f"{self._pending}\n"),
            ]
        colour = design.OK if self._ok else design.DANGER
        icon = design.ICON_SUCCESS if self._ok else design.ICON_ERROR
        return [
            (panel.LABEL, "  RESULT\n\n"),
            (f"bg:{design.SURFACE} fg:{colour} bold", f"  {icon} "),
            (panel.TEXT, f"{self._final}\n"),
        ]

    def details(self) -> StyleAndTextTuples:
        return [
            (panel.TITLE, f"  {self.frame_title}\n\n"),
            (panel.MUTED, f"  {self.hint()}"),
        ]

    def hint(self) -> str:
        return "" if self._ok is not None else "Esc cancel"


class InlineFlow:
    """Runs widget steps in the REPL's own Application, above the prompt box.

    Bind it to the prompt buffer once (``bind``), compose ``container()`` as a
    float and ``add_bindings()`` into the Application's key bindings, then await
    ``select`` / ``prompt_text`` / ``run_status`` from the command-dispatch task.
    """

    def __init__(self) -> None:
        self._stage: _Stage | None = None
        self._future: asyncio.Future | None = None
        self._buffer: Any | None = None
        self._invalidate: Callable[[], None] = lambda: None
        # The in-flight awaitable of a running status step, so Ctrl+C can reach
        # work that is not waiting on a keystroke. None for input steps.
        self._status_work: asyncio.Future | None = None
        self._cancelled = False

    # ── wiring ───────────────────────────────────────────────────────

    def bind(self, buffer: Any, invalidate: Callable[[], None]) -> None:
        self._buffer = buffer
        self._invalidate = invalidate

    def is_active(self) -> bool:
        return self._stage is not None

    def is_masked(self) -> bool:
        return self._stage is not None and self._stage.masked

    def takes_text(self) -> bool:
        """True when the prompt box is this flow's text field for this step."""
        return self._stage is not None and self._stage.entry

    def frame_title(self) -> str:
        return self._stage.frame_title if self._stage is not None else ""

    def prompt_label(self) -> str:
        return self._stage.prompt_label() if self._stage is not None else ""

    def hint(self) -> str:
        return self._stage.hint() if self._stage is not None else ""

    # ── steps ────────────────────────────────────────────────────────

    async def select(
        self,
        title: str,
        options: Sequence[Option],
        *,
        subtitle: str = "",
        filterable: bool = False,
        initial_index: int = 0,
        frame_title: str = "",
    ) -> WidgetResult:
        widget = PaletteSelectWidget(
            title=title,
            options=list(options),
            multiple=False,
            # Always filterable here, whatever the caller asked for. Standalone,
            # a fixed menu can simply not listen for text; but the prompt box
            # accepts typing unconditionally, so the real choice is between
            # "typing filters" and "typed text piles up in a box that ignores
            # it". Even a three-row menu is better off with the former.
            filterable=True,
            subtitle=subtitle,
            initial_index=initial_index,
            frame_title=frame_title,
        )
        return await self._run(_SelectStage(widget))

    async def confirm(self, question: str, *, hint: str = "", default: bool = True) -> WidgetResult:
        """Yes/No, as a two-row palette step so it matches the steps around it."""
        options = [
            Option("yes", "Yes", hint or "Confirm and continue"),
            Option("no", "No", "Leave things as they are"),
        ]
        chosen = await self.select(
            question,
            options,
            initial_index=0 if default else 1,
            frame_title=question,
        )
        if chosen in (BACK, CANCEL):
            return chosen
        return chosen == "yes"

    async def prompt_text(
        self,
        title: str,
        *,
        hint: str = "",
        password: bool = False,
        default: str = "",
        optional: bool = False,
        validate: Callable[[str], str | None] | None = None,
        frame_title: str = "",
    ) -> WidgetResult:
        return await self._run(
            _TextStage(
                title,
                hint=hint,
                password=password,
                default=default,
                optional=optional,
                validate=validate,
                frame_title=frame_title,
            )
        )

    async def run_status(
        self,
        coro: Awaitable[T],
        *,
        pending: str,
        ok: str | Callable[[T], str],
        fail: str | Callable[[T], str],
        is_ok: Callable[[T], bool] | None = None,
        frame_title: str = "",
    ) -> T:
        """Await *coro* behind a spinner drawn in the flow panel.

        Mirrors ``runner.run_with_status`` — including that a raising *coro*
        shows the ✗ frame and then re-raises, so a crash is never mistaken for a
        clean negative result — but without standing up a second Application.
        """

        def _message(spec: str | Callable[[T], str], value: T) -> str:
            return spec(value) if callable(spec) else spec

        stage = _StatusStage(pending, frame_title)
        work = asyncio.ensure_future(coro)

        async def _spin() -> None:
            while not work.done():
                stage.advance()
                self._invalidate()
                await asyncio.sleep(TICK_SECONDS)

        async def _drive() -> Any:
            spinner = asyncio.ensure_future(_spin())
            aborted = False
            try:
                try:
                    value = await work
                except asyncio.CancelledError:
                    stage.settle(ok=False, message="Cancelled.")
                    if self._cancelled:
                        # Ctrl+C, not a shutdown. Converted here rather than
                        # re-raised: _drive() is its own task and was never
                        # itself cancelled, so this only reshapes how *work*'s
                        # cancellation is reported to the awaiting command.
                        aborted = True
                        raise FlowCancelled() from None
                    raise
                except Exception as exc:  # noqa: BLE001 - shown, then re-raised
                    stage.settle(ok=False, message=str(exc))
                    raise
                good = is_ok(value) if is_ok is not None else bool(value)
                stage.settle(ok=good, message=_message(ok if good else fail, value))
                return value
            finally:
                spinner.cancel()
                self._invalidate()
                # Hold the settled frame long enough to be read — but not when
                # the user just asked to get out, where any delay reads as the
                # Ctrl+C not having registered.
                if not aborted:
                    await asyncio.sleep(HOLD_SECONDS)
                self._finish(stage)

        driver = asyncio.ensure_future(_drive())
        self._status_work = work
        self._open(stage)
        try:
            return await driver
        finally:
            self._status_work = None
            self._finish(stage)

    # ── stage lifecycle ──────────────────────────────────────────────

    async def _run(self, stage: _Stage) -> Any:
        if self._buffer is None:
            raise RuntimeError("InlineFlow.bind() must be called before running a step")
        self._future = asyncio.get_running_loop().create_future()
        self._open(stage)
        try:
            return await self._future
        finally:
            self._future = None
            self._finish(stage)

    def _open(self, stage: _Stage) -> None:
        self._stage = stage
        self._cancelled = False
        self._set_buffer(stage.initial_text())
        self._invalidate()

    def _finish(self, stage: _Stage) -> None:
        # Guarded by identity: a step that has already handed the panel to the
        # next one must not close it on the way out of its own ``finally``.
        if self._stage is stage:
            self._stage = None
            self._clear_buffer()
            # The host watches for the panel going away and reclaims the rows
            # it occupied (FullscreenREPLUI._note_overlay_state); all this owes
            # it is a render in which the panel is already gone.
            self._invalidate()

    def _clear_buffer(self) -> None:
        self._set_buffer("")

    def _set_buffer(self, text: str) -> None:
        if self._buffer is not None:
            self._buffer.reset(Document(text, len(text)))

    def _text(self) -> str:
        return self._buffer.text if self._buffer is not None else ""

    def _resolve(self, value: Any) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(value)

    # ── rendering ────────────────────────────────────────────────────

    def _render_results(self) -> StyleAndTextTuples:
        if self._stage is None:
            return []
        self._stage.sync(self._text())
        return self._stage.results()

    def _render_details(self) -> StyleAndTextTuples:
        if self._stage is None:
            return []
        self._stage.sync(self._text())
        return self._stage.details()

    def container(self) -> ConditionalContainer:
        # Same chrome as the standalone palette (interactive.panel) and the
        # same geometry as the `/` command palette float, so a flow reached
        # from a slash command looks like a continuation of it.
        return ConditionalContainer(
            panel.framed(
                panel.two_pane(self._render_results, self._render_details),
                # Callable: the caption changes per step while one frame stays
                # on screen ("CONNECT PROVIDER" → "ANTHROPIC KEY").
                self.frame_title,
            ),
            filter=Condition(self.is_active),
        )

    # ── keys ─────────────────────────────────────────────────────────

    def add_bindings(self, bindings: KeyBindings) -> None:
        active = Condition(self.is_active)

        @bindings.add("up", filter=active, eager=True)
        def _up(event) -> None:
            self._stage.move(-1)
            event.app.invalidate()

        @bindings.add("down", filter=active, eager=True)
        def _down(event) -> None:
            self._stage.move(1)
            event.app.invalidate()

        @bindings.add("enter", filter=active, eager=True)
        def _submit(event) -> None:
            # Deliberately not `buffer.validate_and_handle()`: that would append
            # the text to the REPL's on-disk FileHistory, which for the API-key
            # step means writing the key to ~/.velune in plain text.
            outcome = self._stage.submit(self._text())
            if outcome is KEEP:
                event.app.invalidate()
                return
            self._clear_buffer()
            self._resolve(outcome)
            event.app.invalidate()

        @bindings.add("escape", filter=active, eager=True)
        def _back(event) -> None:
            self._clear_buffer()
            self._resolve(BACK)
            event.app.invalidate()

        # Ctrl+C is deliberately absent: the REPL binds it eagerly too, and two
        # eager bindings on one key resolve to whichever registered last (the
        # REPL's, built after this). It calls `cancel()` below instead, so the
        # precedence is stated in code rather than implied by call order.

    def cancel(self) -> None:
        """Abort the whole flow from whatever step is showing (Ctrl+C).

        Raises :class:`FlowCancelled` into the awaiting command rather than
        resolving the step, so the command unwinds instead of advancing to its
        next step. A no-op when nothing is running, so a stray Ctrl+C at the
        idle prompt still falls through to the REPL's own handling.
        """
        if self._stage is None:
            return
        self._cancelled = True
        self._clear_buffer()

        work = self._status_work
        if work is not None and not work.done():
            # A status step is not waiting on a keystroke — there is no future
            # to fail. Cancel the work instead and let _drive() convert that
            # into FlowCancelled on the way out.
            work.cancel()
            return

        if self._future is not None and not self._future.done():
            self._future.set_exception(FlowCancelled())
