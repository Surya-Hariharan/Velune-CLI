"""Async execution helpers for CLI and orchestration workflows."""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from velune.core.event_loop import get_loop

T = TypeVar("T")


# TODO: Remove after all callers migrate to await
def run_async(awaitable: Awaitable[T]) -> T:
    """Run an awaitable from synchronous CLI code."""
    warnings.warn(
        "run_async() is deprecated and will be removed. Use await directly or event_loop.submit().",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_loop().run_until_complete(awaitable)


def async_command(fn: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    """Adapt an async command handler to a synchronous CLI callback."""

    @wraps(fn)
    def wrapper(*args, **kwargs) -> T:
        return run_async(fn(*args, **kwargs))

    return wrapper
