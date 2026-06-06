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
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "submit() called from a running event loop — await the coroutine directly instead."
    )
