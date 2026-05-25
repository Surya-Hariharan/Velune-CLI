"""Module-level event loop manager for Velune process lifetime."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
logger = logging.getLogger("velune")


def get_loop() -> asyncio.AbstractEventLoop:
    """Get the process-wide event loop singleton."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def submit(coro: Awaitable[T]) -> T:
    """Submit a coroutine from sync context to the running loop."""
    loop = get_loop()
    if loop.is_running():
        # We are inside an async context; caller should have used await
        raise RuntimeError(
            "submit() called from running loop. Use 'await' directly."
        )

    coro_name = getattr(coro, "__name__", None) or type(coro).__name__
    logger.debug(f"Submitting {coro_name} to event loop")

    start_time = time.perf_counter()
    try:
        return loop.run_until_complete(coro)
    finally:
        elapsed = time.perf_counter() - start_time
        logger.debug(f"Total async execution time: {elapsed:.4f}s")
