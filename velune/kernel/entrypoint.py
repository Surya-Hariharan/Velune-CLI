"""Single async runtime entry point for Velune.

``asyncio.run()`` is called **exactly once** in the entire codebase — inside
``run_async()`` below.  Every module that needs to bridge from sync to async
(CLI command callbacks, deprecated sync retrieval helpers, the daemon server)
imports and calls ``run_async()`` instead of calling ``asyncio.run()``
directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")
_logger = logging.getLogger(__name__)


def _install_uvloop() -> None:
    """Swap the default asyncio event-loop policy for uvloop if available."""
    try:
        import uvloop  # type: ignore[import-untyped]

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        _logger.debug("uvloop event-loop policy installed.")
    except ImportError:
        pass


def run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run *coro* to completion from a **synchronous** call site.

    This is the **only** place in the entire Velune codebase that calls
    ``asyncio.run()``.  All other callers must import and use this function.

    Raises:
        RuntimeError: If called from within a running event loop.  Callers
            inside an async context must ``await`` the coroutine directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "run_async() called from a running event loop — "
            "await the coroutine directly instead."
        )
    _install_uvloop()
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        raise
    except SystemExit:
        raise


async def _async_main(runtime: Any) -> None:
    """Top-level coroutine: run the interactive REPL session."""
    from velune.cli.repl import VeluneREPL

    repl = VeluneREPL(runtime)
    await repl.run()


def launch(runtime: Any) -> None:
    """Start the full interactive Velune session from a synchronous Typer callback.

    Catches ``KeyboardInterrupt`` (Ctrl-C outside the REPL loop) so Typer sees
    a clean exit.  ``SystemExit`` is re-raised so ``typer.Exit`` works normally.
    """
    try:
        run_async(_async_main(runtime))
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
