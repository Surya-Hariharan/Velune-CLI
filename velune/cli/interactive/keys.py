"""Centralized Esc/Ctrl-C key bindings shared by every interactive widget.

Every other key (arrows, space, enter, type-to-filter) is widget-specific and
lives on the widget itself (see ``widget.py``). Esc (back) and Ctrl-C (cancel)
are the two bindings every screen needs identically, so they are defined here
exactly once instead of being hand-rolled per widget as they were in
``picker.py``, ``provider_ui.py``, and ``fullscreen.py``.

Both are registered ``eager=True``. This is not a style choice: on Windows,
``asyncio`` has no ``SIGINT`` handler, so ``prompt_toolkit.Application`` never
sees a ``KeyboardInterrupt`` while ``run_async()`` is awaiting — an eager
key binding is the *only* mechanism that reliably intercepts Ctrl-C on that
platform. ``eager=True`` also guarantees Esc/Ctrl-C win over whatever the
currently focused control (a ``TextArea``'s buffer, a widget's own arrow-key
bindings) has bound for the same key.
"""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.key_binding import KeyBindings


def common_bindings(
    *,
    on_cancel: Callable[[], None],
    on_back: Callable[[], None] | None = None,
) -> KeyBindings:
    """Return a KeyBindings with only Ctrl-C (cancel) and optionally Esc (back).

    Pass ``on_back=None`` for screens that have nothing to go back to (e.g.
    the first screen of a standalone widget with no wizard history).
    """
    kb = KeyBindings()

    @kb.add("c-c", eager=True)
    def _cancel(event) -> None:
        on_cancel()

    if on_back is not None:

        @kb.add("escape", eager=True)
        def _back(event) -> None:
            on_back()

    return kb
