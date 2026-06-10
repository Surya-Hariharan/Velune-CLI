from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the running event loop, or a freshly created one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.new_event_loop()


def submit(coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* to completion from a synchronous call site.

    Raises RuntimeError if called from within a running event loop — callers
    inside an async context should await the coroutine directly.

    Delegates to ``velune.kernel.entrypoint.run_async`` so that
    ``asyncio.run()`` is called from exactly one place in the codebase.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        from velune.kernel.entrypoint import run_async

        return run_async(coro)
    raise RuntimeError(
        "submit() called from a running event loop — await the coroutine directly instead."
    )
