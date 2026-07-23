"""Single async runtime entry point for Velune.

``asyncio.run`` is called **exactly once** in the entire codebase — inside
``run_async()`` below.  Every module that needs to bridge from sync to async
(CLI command callbacks, deprecated sync retrieval helpers, the daemon server)
imports and calls ``run_async()`` instead of calling ``asyncio.run``
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
    ``asyncio.run``.  All other callers must import and use this function.

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
            "run_async() called from a running event loop — await the coroutine directly instead."
        )
    _install_uvloop()
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        raise
    except SystemExit:
        raise


async def _async_main(runtime: Any, *, plain: bool = False) -> None:
    """Top-level coroutine: own the subsystem lifecycle around the REPL.

    Startup initializes every lifecycle-managed subsystem (schema migrations,
    storage pools, background workers). Shutdown is guaranteed by the
    ``finally`` block regardless of how the REPL ends — clean /exit, double
    Ctrl+C, or an unexpected error — so no SQLite handles, embedding workers,
    or provider connections ever outlive the session.
    """
    from velune.cli.repl import VeluneREPL

    lifecycle = None
    try:
        lifecycle = runtime.container.get("runtime.lifecycle")
        # First pass initializes the Tier-0 lifecycle subsystems (bus, providers,
        # models, trace sink) — all cheap. Tier-1 subsystems are initialized by
        # the background warm-up's second startup() pass once they register.
        await lifecycle.startup()
    except Exception as exc:
        _logger.warning("Subsystem startup incomplete, continuing degraded: %s", exc)

    # Start the proactive issue watcher (subscribes to bus + runs periodic checks).
    watcher = None
    try:
        from velune.proactive.watcher import ProactiveWatcher

        bus = runtime.container.get("runtime.bus")
        alert_store = runtime.container.get("runtime.alert_store")
        job_registry = runtime.container.get("runtime.job_registry")
        try:
            health_monitor = runtime.container.get("runtime.provider_health_monitor")
        except Exception:
            health_monitor = None

        profile = runtime.container.get_optional("runtime.profile")
        periodic_interval = (
            ProactiveWatcher.PERIODIC_INTERVAL_S * profile.background_poll_scale
            if profile
            else None
        )

        watcher = ProactiveWatcher(
            bus=bus,
            alert_store=alert_store,
            job_registry=job_registry,
            health_monitor=health_monitor,
            periodic_interval_s=periodic_interval,
        )
        runtime.container.register_instance("runtime.proactive_watcher", watcher)
        await watcher.start()
    except Exception as exc:
        _logger.warning("ProactiveWatcher failed to start, continuing without it: %s", exc)

    repl = VeluneREPL(runtime)

    # Warm the expensive Tier-1 subsystems in the background so the prompt is
    # interactive immediately. When they finish, initialize their lifecycle and
    # let the REPL pick up the now-available orchestrator/models.
    async def _warm_and_finalize() -> None:
        from velune.core.runtime import warm_background

        await warm_background(runtime)
        if lifecycle is not None:
            try:
                await lifecycle.startup()  # init Tier-1 lifecycle subsystems
            except Exception as exc:
                _logger.warning("Tier-1 lifecycle startup incomplete: %s", exc)
        try:
            repl.on_warm_complete()
        except Exception as exc:
            _logger.debug("REPL warm-complete hook failed: %s", exc)

    if getattr(runtime, "env", None) is not None:
        from velune.core.task_registry import track

        track(asyncio.create_task(_warm_and_finalize(), name="velune.warm_background"))

    try:
        await repl.run(plain=plain)
    finally:
        if watcher is not None:
            try:
                await watcher.stop()
            except Exception as exc:
                _logger.error("ProactiveWatcher shutdown error: %s", exc)
        if lifecycle is not None:
            try:
                await lifecycle.shutdown()
            except Exception as exc:
                _logger.error("Subsystem shutdown error: %s", exc)


def launch(runtime: Any, *, plain: bool = False) -> None:
    """Start the full interactive Velune session from a synchronous Typer callback.

    ``plain=True`` runs the linear, non-alt-screen mode (``velune --plain``)
    instead of the fullscreen alternate-screen UI — see
    :meth:`velune.cli.repl.VeluneREPL.run`.

    Catches ``KeyboardInterrupt`` (Ctrl-C outside the REPL loop) so Typer sees
    a clean exit.  ``SystemExit`` is re-raised so ``typer.Exit`` works normally.

    Any other exception is a genuine crash: if the user has opted in (see
    ``velune.cli.crash_reporter``, off by default), a redacted local report is
    written before the exception is re-raised unchanged — this never
    suppresses or alters what the user sees on a crash, it only optionally
    keeps a local copy of it.
    """
    try:
        run_async(_async_main(runtime, plain=plain))
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception as exc:
        try:
            from velune.cli.crash_reporter import write_crash_report

            report_path = write_crash_report(exc)
            if report_path is not None:
                _logger.error("Crash report written to %s", report_path)
        except Exception:
            pass
        raise
