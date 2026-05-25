"""Structured concurrency manager for tracking, timing out, and cancelling background tasks."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")
logger = logging.getLogger("velune.core.task_registry")


class BackgroundTaskRegistry:
    """Tracks in-flight asyncio tasks with timeout and cancellation support."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        name: str,
        coro: Awaitable[T],
        timeout_seconds: float = 60.0,
        on_error: Callable[[Exception], None] | None = None,
    ) -> asyncio.Task:
        """Submit a named background task with timeout and error capture."""
        from velune.core.event_loop import get_loop

        async def _wrapped() -> Any:
            start_time = time.perf_counter()
            try:
                result = await asyncio.wait_for(coro, timeout=timeout_seconds)
                duration = time.perf_counter() - start_time
                logger.debug("Background task '%s' completed successfully in %.4fs", name, duration)
                return result
            except TimeoutError:
                logger.warning("Background task '%s' timed out after %.1fs", name, timeout_seconds)
            except asyncio.CancelledError:
                duration = time.perf_counter() - start_time
                logger.debug("Background task '%s' was cancelled after %.4fs", name, duration)
                raise
            except Exception as exc:
                logger.error("Background task '%s' failed: %s", name, exc)
                if on_error:
                    try:
                        on_error(exc)
                    except Exception as inner_err:
                        logger.error("Error handler for task '%s' failed: %s", name, inner_err)
            finally:
                with self._lock:
                    self._tasks.pop(name, None)

        loop = get_loop()
        event = threading.Event()
        task = None

        def _create():
            nonlocal task
            task = loop.create_task(_wrapped(), name=name)
            with self._lock:
                self._tasks[name] = task
            event.set()

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop or not loop.is_running():
            task = loop.create_task(_wrapped(), name=name)
            with self._lock:
                self._tasks[name] = task
        else:
            loop.call_soon_threadsafe(_create)
            event.wait(timeout=5.0)  # Wait for creation thread-safely

        return task

    async def cancel_all(self, timeout: float = 5.0) -> None:
        """Cancel all pending tasks and wait for completion."""
        with self._lock:
            tasks = list(self._tasks.values())

        for task in tasks:
            task.cancel()

        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except TimeoutError:
                logger.warning("cancel_all() timed out waiting for tasks to cancel after %.1fs", timeout)

        with self._lock:
            self._tasks.clear()

    def pending_count(self) -> int:
        """Expose pending tasks count for health checks."""
        with self._lock:
            return len([t for t in self._tasks.values() if not t.done()])
