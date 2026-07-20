"""Subsystem lifecycle manager and transition tracker."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from velune.kernel.schemas import ComponentStatus

if TYPE_CHECKING:
    from velune.kernel.config import VeluneConfig

logger = logging.getLogger("velune.kernel.lifecycle")


@runtime_checkable
class Subsystem(Protocol):
    """Lifecycle protocol for subsystems that need explicit startup or shutdown."""

    async def initialize(self) -> None:
        """Startup procedures for the component."""
        ...

    async def shutdown(self) -> None:
        """Teardown procedures for the component."""
        ...


class LifecycleCoordinator:
    """Orchestrates structured startup, health transitions, and graceful shutdown of subsystems."""

    def __init__(self) -> None:
        self._components: dict[str, Subsystem] = {}
        self._states: dict[str, ComponentStatus] = {}
        self._started = False
        self._config_validated = False
        self.container: Any = None
        self._config: VeluneConfig | None = None

    def set_config(self, config: VeluneConfig) -> None:
        """Attach a VeluneConfig so startup() can validate it before initialising subsystems."""
        self._config = config

    def register(self, name: str, component: Subsystem) -> None:
        """Register a component for active lifecycle tracking."""
        self._components[name] = component
        self._states[name] = ComponentStatus.UNINITIALIZED
        logger.debug("Component '%s' registered for lifecycle tracking.", name)

    def get_status(self, name: str) -> ComponentStatus:
        """Retrieve the current state of a registered component."""
        return self._states.get(name, ComponentStatus.UNINITIALIZED)

    def set_status(self, name: str, status: ComponentStatus) -> None:
        """Explicitly set a component status."""
        if name in self._states:
            self._states[name] = status
            logger.debug("Component '%s' transitioned to state: %s", name, status.value)

    async def startup(self) -> None:
        """Initialize any not-yet-started registered subsystems.

        This is **incremental and idempotent**: it may be called more than once
        as subsystems register across startup tiers (Tier-0 synchronously, then
        Tier-1 in the background warm-up). Each call initializes only the
        components still in ``UNINITIALIZED`` state, so a later call picks up the
        background subsystems registered after the first call.

        If a :class:`~velune.kernel.config.VeluneConfig` was attached via
        :meth:`set_config`, it is validated once, on the first call. Any
        ``CRITICAL`` validation errors abort startup immediately with a
        descriptive exception.
        """
        if self._config is not None and not self._config_validated:
            self._config_validated = True
            from velune.kernel.config import ConfigValidationError  # noqa: F401

            errors = self._config.validate()
            critical = [e for e in errors if e.severity == "CRITICAL"]
            if critical:
                for err in critical:
                    logger.critical(
                        "Config validation failed — %s = %r: %s",
                        err.field,
                        err.value,
                        err.reason,
                    )
                summary = "; ".join(f"{e.field}: {e.reason}" for e in critical)
                raise RuntimeError(
                    f"VeluneConfig has {len(critical)} critical error(s). "
                    f"Startup aborted. Details: {summary}"
                )
            for warn in errors:
                if warn.severity != "CRITICAL":
                    logger.warning(
                        "Config warning — %s = %r: %s",
                        warn.field,
                        warn.value,
                        warn.reason,
                    )

        self._started = True
        logger.info("Initializing Velune systems...")
        # Snapshot: the background warm-up may register further subsystems while
        # this coroutine awaits an initialize(); a later startup() call picks
        # those up. Iterating a snapshot avoids "dict changed size" errors.
        failed: list[str] = []
        for name, comp in list(self._components.items()):
            if self._states.get(name) != ComponentStatus.UNINITIALIZED:
                continue

            self._states[name] = ComponentStatus.INITIALIZING
            try:
                if hasattr(comp, "initialize") and callable(comp.initialize):
                    await comp.initialize()
                self._states[name] = ComponentStatus.HEALTHY
                logger.info("Subsystem '%s' initialized successfully.", name)
            except Exception as e:
                # Record and continue rather than re-raising. Components are
                # initialized in a single ordered pass, so aborting here left
                # every *later* subsystem permanently UNINITIALIZED — and both
                # call sites swallow the exception, so the session carried on
                # silently half-warm with no way to tell it apart from cold.
                # One broken subsystem should cost that subsystem, not the rest.
                self._states[name] = ComponentStatus.FAILED
                failed.append(name)
                logger.error("Subsystem '%s' failed to initialize: %s", name, e, exc_info=True)

        if failed:
            logger.warning(
                "Continuing with %d degraded subsystem(s): %s",
                len(failed),
                ", ".join(failed),
            )

    async def shutdown(self) -> None:
        """Shut down all registered subsystems gracefully in reverse order."""
        # Fire-and-forget tasks must stop before any component closes: several of
        # them write to the SQLite pool and embedding pipeline torn down below.
        # The REPL also does this, but run() can raise before reaching its
        # shutdown path, so this is the backstop that always executes.
        try:
            from velune.core.task_registry import cancel_tracked

            await cancel_tracked(timeout=10.0)
        except Exception as e:
            logger.error("Failed to cancel tracked tasks during shutdown: %s", e)

        if self.container and self.container.has("runtime.task_registry"):
            try:
                task_registry = self.container.get("runtime.task_registry")
                await task_registry.cancel_all(timeout=10.0)
            except Exception as e:
                logger.error("Failed to cancel background tasks during shutdown: %s", e)

        logger.info("Shutting down Velune systems...")
        for name in reversed(list(self._components.keys())):
            comp = self._components[name]
            self._states[name] = ComponentStatus.SHUTTING_DOWN
            try:
                if hasattr(comp, "shutdown") and callable(comp.shutdown):
                    await comp.shutdown()
                elif hasattr(comp, "stop") and callable(comp.stop):
                    if asyncio.iscoroutinefunction(comp.stop):
                        await comp.stop()
                    else:
                        comp.stop()
                self._states[name] = ComponentStatus.SHUTDOWN
                logger.info("Subsystem '%s' shut down successfully.", name)
            except Exception as e:
                self._states[name] = ComponentStatus.FAILED
                logger.error("Error shutting down subsystem '%s': %s", name, e)

        self._components.clear()
        self._states.clear()
        self._started = False
