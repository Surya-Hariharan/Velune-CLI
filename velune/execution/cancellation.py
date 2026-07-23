"""A signal path for killing an already-running worker-thread subprocess.

``asyncio.Task.cancel()`` cannot interrupt work already running inside
``asyncio.to_thread`` — the ``CancelledError`` is only delivered once the
thread function returns, so a shell tool call kept running silently past a
Ctrl+C until its own timeout (see ``SubprocessSandbox.execute``'s poll loop
and ``InterruptController.cancel_foreground``). This module is the fix: a
plain, thread-safe registry of ``threading.Event``s that a synchronous
Ctrl+C handler can set directly, independent of asyncio's task-cancellation
machinery — the sandbox's own poll loop (already running every 0.05s) checks
its event and kills the process tree immediately instead of waiting for the
awaiting coroutine to be told.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_active: set[threading.Event] = set()


def register(event: threading.Event) -> None:
    with _lock:
        _active.add(event)


def unregister(event: threading.Event) -> None:
    with _lock:
        _active.discard(event)


def cancel_all() -> None:
    """Set every currently registered event — call on Ctrl+C."""
    with _lock:
        events = list(_active)
    for event in events:
        event.set()
