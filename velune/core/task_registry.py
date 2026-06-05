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
        self._submitted_count = 0
        self._dropped_count = 0

    def submit(
        self,
        name: str,
        coro: Awaitable[T],
        timeout_seconds: float = 60.0,
        on_error: Callable[[Exception], None] | None = None,
    ) -> asyncio.Task | None:
        """Submit a named background task from an async context only."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error(
                "BackgroundTaskRegistry.submit('%s') called outside an async context. "
                "Background tasks must be submitted from within a running event loop.",
                name
            )
            with self._lock:
                self._dropped_count += 1
            return None
        
        with self._lock:
            self._submitted_count += 1
        
        async def _wrapped() -> Any:
            try:
                result = await asyncio.wait_for(coro, timeout=timeout_seconds)
                logger.debug("Background task '%s' completed.", name)
                return result
            except TimeoutError:
                logger.warning("Background task '%s' timed out after %.1fs", name, timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Background task '%s' failed: %s", name, exc)
                if on_error:
                    try:
                        on_error(exc)
                    except Exception as inner:
                        logger.error("Error handler for task '%s' failed: %s", name, inner)
            finally:
                with self._lock:
                    self._tasks.pop(name, None)
        
        task = running_loop.create_task(_wrapped(), name=name)
        with self._lock:
            self._tasks[name] = task
        return task

    async def cancel_all(self, timeout: float = 5.0) -> None:
        """Cancel all pending tasks and wait for completion."""
        with self._lock:
            tasks = [t for t in self._tasks.values() if not t.done()]
        
        if not tasks:
            return
        
        for task in tasks:
            task.cancel()
        
        # Give tasks a chance to handle CancelledError
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        
        if pending:
            logger.warning(
                "%d background tasks did not cancel within %.1fs",
                len(pending), timeout
            )
        
        # Shutdown stats reporting of dropped tasks
        stats = self.stats()
        if stats["dropped"] > 0:
            logger.info("Task registry shutdown: %d dropped tasks reported.", stats["dropped"])
            
        with self._lock:
            self._tasks.clear()

    def pending_count(self) -> int:
        """Expose pending tasks count for health checks."""
        with self._lock:
            return len([t for t in self._tasks.values() if not t.done()])

    def is_healthy(self) -> bool:
        """Check if task registry is healthy."""
        try:
            loop = asyncio.get_running_loop()
            loop_alive = loop.is_running()
        except Exception:
            loop_alive = False
        return loop_alive and self.pending_count() < 50

    def stats(self) -> dict[str, int]:
        """Return task submission telemetry stats."""
        with self._lock:
            return {
                "submitted": self._submitted_count,
                "dropped": self._dropped_count,
            }
