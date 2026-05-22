"""Subsystem lifecycle manager and transition tracker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Protocol, runtime_checkable
from velune.kernel.schemas import ComponentStatus

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
        self._components: Dict[str, Subsystem] = {}
        self._states: Dict[str, ComponentStatus] = {}

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
        """Initialize all registered subsystems sequentially."""
        logger.info("Initializing Velune systems...")
        for name, comp in self._components.items():
            if self._states[name] != ComponentStatus.UNINITIALIZED:
                continue

            self._states[name] = ComponentStatus.INITIALIZING
            try:
                if hasattr(comp, "initialize") and callable(comp.initialize):
                    await comp.initialize()
                self._states[name] = ComponentStatus.HEALTHY
                logger.info("Subsystem '%s' initialized successfully.", name)
            except Exception as e:
                self._states[name] = ComponentStatus.FAILED
                logger.critical("Subsystem '%s' failed to initialize: %s", name, e)
                raise e

    async def shutdown(self) -> None:
        """Shut down all registered subsystems gracefully in reverse order."""
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
