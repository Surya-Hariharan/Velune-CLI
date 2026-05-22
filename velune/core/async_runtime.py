"""Async execution helpers for CLI and orchestration workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar


T = TypeVar("T")


def run_async(awaitable: Awaitable[T]) -> T:
    """Run an awaitable from synchronous CLI code."""

    return asyncio.run(awaitable)


def async_command(fn: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    """Adapt an async command handler to a synchronous CLI callback."""

    @wraps(fn)
    def wrapper(*args, **kwargs) -> T:
        return run_async(fn(*args, **kwargs))

    return wrapper