"""Run a coroutine to completion with a deadline, without ``asyncio.wait_for``.

Before Python 3.12, ``wait_for`` runs the awaited coroutine in a *child* task,
so the task the coroutine sees as ``current_task()`` is not the one
``asyncio.run`` created, and that outer task shows up in ``all_tasks()`` as a
phantom leftover. Cancellation tests that assert "nothing is still running"
then fail on 3.10/3.11 only. Awaiting the coroutine directly in the main task
and scheduling its cancellation keeps the task set identical on every version.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_with_deadline(coro: Coroutine[Any, Any, T], timeout: float = 10) -> T:
    async def main() -> T:
        task = asyncio.current_task()
        assert task is not None
        handle = asyncio.get_running_loop().call_later(timeout, task.cancel)
        try:
            return await coro
        finally:
            handle.cancel()

    return asyncio.run(main())
