"""Interrupt lifecycle for the Velune REPL.

Velune has two distinct Ctrl+C surfaces:

1. **At the prompt** — prompt_toolkit owns the terminal in raw mode, so
   Ctrl+C arrives as a key event, never as SIGINT. The REPL's key binding
   consults :meth:`InterruptController.note_interrupt` to implement the
   "press Ctrl+C again to exit" double-press contract.

2. **During generation / council runs** — the terminal is in cooked mode and
   Ctrl+C raises SIGINT. The default Python handler raises
   ``KeyboardInterrupt`` on the main thread, which on Windows tears down the
   running event loop instead of surfacing inside the awaiting coroutine.
   :class:`InterruptController` replaces that handler for the lifetime of the
   REPL: while a *foreground* task is registered, SIGINT cancels that task
   (via ``loop.call_soon_threadsafe``) so generation stops cleanly and the
   loop, sessions, and memory state all survive.

The controller never owns shutdown itself — it only converts interrupts into
structured cancellation and exposes the double-press window so the REPL can
decide when an exit was actually requested.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import asynccontextmanager

_log = logging.getLogger("velune.cli.interrupts")


class InterruptController:
    """Converts SIGINT into foreground-task cancellation with a double-press exit window."""

    #: Seconds within which a second Ctrl+C is treated as an exit request.
    exit_window_seconds: float = 2.0

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._foreground_task: asyncio.Task | None = None
        self._prev_handler: object = None
        self._installed = False
        self._last_interrupt_at = float("-inf")
        self._user_cancelled = False

    # ── Signal handler lifetime ──────────────────────────────────────────

    def install(self) -> None:
        """Install the SIGINT handler. Must be called from a running loop."""
        if self._installed:
            return
        self._loop = asyncio.get_running_loop()
        try:
            self._prev_handler = signal.signal(signal.SIGINT, self._on_sigint)
            self._installed = True
        except ValueError:
            # Not on the main thread (e.g. tests running the REPL in a
            # worker thread) — fall back to default KeyboardInterrupt flow.
            _log.debug("SIGINT handler not installed: not on main thread")

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            signal.signal(signal.SIGINT, self._prev_handler or signal.default_int_handler)
        except (ValueError, TypeError):
            pass
        self._installed = False
        self._loop = None

    def _on_sigint(self, signum, frame) -> None:  # noqa: ANN001 — signal API
        self.note_interrupt()
        task = self._foreground_task
        if task is not None and not task.done():
            self._user_cancelled = True
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
            return
        # No foreground work — preserve default semantics so the REPL's
        # own KeyboardInterrupt handling (and Typer's) still applies.
        raise KeyboardInterrupt

    # ── Double-press exit window ─────────────────────────────────────────

    def note_interrupt(self) -> bool:
        """Record an interrupt; return True when it lands inside the exit window."""
        now = time.monotonic()
        is_double = (now - self._last_interrupt_at) <= self.exit_window_seconds
        self._last_interrupt_at = now
        return is_double

    @property
    def exit_hint_active(self) -> bool:
        """True while the 'press Ctrl+C again to exit' hint should be visible."""
        return (time.monotonic() - self._last_interrupt_at) <= self.exit_window_seconds

    def reset_exit_window(self) -> None:
        """Clear the double-press window (e.g. after the user typed something)."""
        self._last_interrupt_at = float("-inf")

    # ── Foreground task registration ─────────────────────────────────────

    @asynccontextmanager
    async def foreground(self):
        """Mark the current task as interruptible foreground work.

        While active, SIGINT cancels *this* task instead of raising
        ``KeyboardInterrupt`` on the main thread. Use together with
        :meth:`consume_user_cancelled` to distinguish a user interrupt from
        a genuine shutdown cancellation:

            try:
                async with controller.foreground():
                    await generate()
            except asyncio.CancelledError:
                if not controller.consume_user_cancelled():
                    raise  # real shutdown — propagate
                asyncio.current_task().uncancel()
        """
        self._foreground_task = asyncio.current_task()
        self._user_cancelled = False
        try:
            yield self
        finally:
            self._foreground_task = None

    def consume_user_cancelled(self) -> bool:
        """Return True if the last cancellation came from Ctrl+C, then reset."""
        was_user = self._user_cancelled
        self._user_cancelled = False
        return was_user

    @property
    def has_foreground(self) -> bool:
        return self._foreground_task is not None
