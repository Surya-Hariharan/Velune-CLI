"""Standalone widget runner — one private ``Application`` per call.

For interactive prompts that aren't part of the onboarding wizard chrome
(REPL palettes, ``/session``/``/project`` pickers, one-off confirmations).
Wizard stages use ``chrome.WizardController.run_widget`` instead, which
reuses a single long-lived ``Application`` across the whole run.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import merge_key_bindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from velune.cli import design
from velune.cli.interactive.keys import common_bindings
from velune.cli.interactive.result import BACK, CANCEL
from velune.cli.interactive.tty import is_interactive_tty
from velune.cli.interactive.widget import Widget
from velune.cli.interactive.widgets.status import (
    HOLD_SECONDS,
    TICK_SECONDS,
    StatusWidget,
)
from velune.cli.interactive.widgets.text import TextInputWidget

T = TypeVar("T")


def _plain_mark(ok: bool) -> str:
    """A ✓/✗ the *current* stdout can actually encode.

    The non-TTY path writes with a bare ``print``, so it inherits whatever
    encoding the redirected stream has — on Windows that is frequently cp1252,
    which cannot represent U+2713 and raises ``UnicodeEncodeError`` mid-command.
    Fall back to ASCII rather than crash a piped run over a decorative glyph.
    """
    glyph = design.ICON_SUCCESS if ok else design.ICON_ERROR
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        glyph.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "[ok]" if ok else "[!!]"
    return glyph


def _footer_window(widget: Widget | TextInputWidget) -> Window:
    def _render():
        return [(f"fg:{design.FAINT}", f"\n  {widget.footer_hint()}")]

    return Window(FormattedTextControl(_render), dont_extend_height=True)


async def run_standalone(
    widget: Widget | TextInputWidget,
    *,
    input: Any | None = None,
    output: Any | None = None,
) -> Any:
    """Run *widget* full-screen-off in its own ``Application``.

    Returns whatever the widget submits, or the ``BACK``/``CANCEL`` sentinel
    if the user pressed Esc / Ctrl-C. Esc is only wired when the widget's
    ``on_back`` was left at its no-op default — callers that want "no back
    target" simply don't override it, and Esc still resolves to ``BACK`` so
    the caller can decide whether that's meaningful.

    ``input``/``output`` are forwarded to ``Application`` as-is (``None``
    keeps prompt_toolkit's own defaults) — the seam exists so tests can drive
    a real widget through synthetic key events via
    ``prompt_toolkit.input.create_pipe_input`` instead of mocking the widget.
    """
    result: dict[str, Any] = {}

    def _submit(value: Any) -> None:
        result["value"] = value
        app.exit()

    def _back() -> None:
        result["value"] = BACK
        app.exit()

    def _cancel() -> None:
        result["value"] = CANCEL
        app.exit()

    widget.on_submit = _submit
    widget.on_back = _back
    widget.on_cancel = _cancel

    is_text = isinstance(widget, TextInputWidget)

    if is_text:
        body = widget.container
        # No Ctrl-C-cancel binding here: this field is exactly where a user
        # pastes/copies a value (an API key above all), and eagerly treating
        # Ctrl-C as "abandon the flow" fights that. Esc still backs out.
        kb = common_bindings(on_cancel=None, on_back=_back)
    else:
        # A widget may supply its own container when a single Window can't
        # express its layout — PaletteSelectWidget's framed two-column panel,
        # for one. Key bindings are registered on the Application either way,
        # so they work without the control being focusable.
        own_container = getattr(widget, "container", None)
        body = (
            own_container
            if own_container is not None
            else Window(FormattedTextControl(widget.render, focusable=True))
        )
        kb = merge_key_bindings(
            [widget.key_bindings(), common_bindings(on_cancel=_cancel, on_back=_back)]
        )

    # Panelled widgets carry their key hints inside their own chrome; a second
    # copy under the frame would just repeat them.
    parts = [body] if getattr(widget, "shows_own_hints", False) else [body, _footer_window(widget)]
    layout = Layout(HSplit(parts))

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        mouse_support=True,
        input=input,
        output=output,
    )
    await app.run_async()
    return result.get("value", CANCEL)


async def run_with_status(
    coro: Awaitable[T],
    *,
    pending: str,
    ok: str | Callable[[T], str],
    fail: str | Callable[[T], str],
    is_ok: Callable[[T], bool] | None = None,
    input: Any | None = None,
    output: Any | None = None,
) -> T:
    """Await *coro* behind a spinner, then show a ✓/✗ frame, and return its value.

    ``is_ok`` decides which frame to show. It exists because the interesting
    cases here — ``ValidationResult`` above all — report failure as a *value*,
    not an exception, so "did it raise?" is the wrong question. Defaults to
    plain truthiness.

    ``ok``/``fail`` may be strings, or callables taking the result (so the
    message can name what came back: "Verified — 8 models available").

    If *coro* raises, the ✗ frame shows the exception and the exception is
    re-raised: a caller must never mistake a crash for a clean "invalid key".

    Outside an interactive TTY there is no Application to drive — the coroutine
    is awaited plainly and the outcome printed as one line, so piped and CI
    invocations behave and stay readable.
    """

    def _message(spec: str | Callable[[T], str], value: T) -> str:
        return spec(value) if callable(spec) else spec

    def _succeeded(value: T) -> bool:
        return is_ok(value) if is_ok is not None else bool(value)

    if not is_interactive_tty():
        value = await coro
        good = _succeeded(value)
        text = _message(ok if good else fail, value)
        print(f"  {_plain_mark(good)} {text}")
        return value

    widget = StatusWidget(pending=pending)
    work = asyncio.ensure_future(coro)

    app: Application = Application(
        layout=Layout(
            HSplit(
                [
                    Window(FormattedTextControl(widget.render), dont_extend_height=True),
                    _footer_window(widget),
                ]
            )
        ),
        # Esc/Ctrl-C cancel the in-flight work rather than silently orphaning it.
        key_bindings=common_bindings(on_cancel=work.cancel, on_back=work.cancel),
        full_screen=False,
        mouse_support=False,
        input=input,
        output=output,
    )

    async def _spin() -> None:
        """Advance the spinner until the work settles."""
        while not work.done():
            widget.advance()
            app.invalidate()
            await asyncio.sleep(TICK_SECONDS)

    async def _drive() -> None:
        spinner = asyncio.ensure_future(_spin())
        try:
            try:
                value = await work
            except asyncio.CancelledError:
                widget.settle(ok=False, message="Cancelled.")
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced in the frame, then re-raised
                widget.settle(ok=False, message=str(exc))
                raise
            else:
                good = _succeeded(value)
                widget.settle(ok=good, message=_message(ok if good else fail, value))
        finally:
            spinner.cancel()
            app.invalidate()
            # Hold the terminal frame long enough to be read, then close.
            await asyncio.sleep(HOLD_SECONDS)
            if app.is_running:
                app.exit()

    driver = asyncio.ensure_future(_drive())
    try:
        await app.run_async()
    finally:
        # The driver owns the outcome; awaiting it here propagates a failure or
        # a cancellation to our caller instead of leaving a detached task.
        await asyncio.gather(driver, return_exceptions=True)

    return await work
