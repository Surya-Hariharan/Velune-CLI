"""Kernel component registry — string-keyed ServiceContainer with lazy factories."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger("velune.kernel.registry")

T = TypeVar("T")


class ServiceContainer:
    """Component registry container supporting string-based lazy factories.

    Also tracks per-key *readiness*. Services that are bootstrapped lazily in a
    background warm-up task (Tier 1) are not present the instant the prompt
    appears; callers that genuinely need such a service can ``await
    wait_ready(key)`` instead of crashing on a missing registration. Tier-0
    services are registered synchronously before the prompt and are therefore
    ready immediately.
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._factories: dict[str, Callable[[], Any]] = {}
        self._singletons: dict[str, Any] = {}
        # Readiness gates for background-initialized services. An Event is
        # created lazily on first reference so ``wait_ready`` works even when
        # called before the service has been registered.
        self._ready_events: dict[str, asyncio.Event] = {}

    def register(self, name: str, factory: Callable[[], T], singleton: bool = True) -> None:
        """Register a service factory by key."""
        if singleton:
            self._factories[name] = factory
        else:
            self._services[name] = factory
        logger.debug("Registered factory for service: '%s' (singleton=%s)", name, singleton)

    def register_instance(self, name: str, instance: Any) -> None:
        """Register a concrete service instance directly."""
        self._singletons[name] = instance
        # A concrete registration is, by definition, ready. Signal any waiters.
        if name in self._ready_events:
            self._ready_events[name].set()
        logger.debug("Registered direct instance for service: '%s'", name)

    def hot_swap(self, name: str, replacement: Any) -> None:
        """Dynamically replace an active service instance or registry factory."""
        logger.info("Hot-swapping service: '%s'", name)
        self._singletons.pop(name, None)
        self._factories.pop(name, None)
        self._services.pop(name, None)  # Clear cached singleton too
        self._singletons[name] = replacement

    def get(self, name: str) -> Any:
        """Retrieve a service instance by key. Resolves lazy factories if needed."""
        if name in self._singletons:
            return self._singletons[name]

        if name in self._factories:
            if name not in self._services:
                factory = self._factories[name]
                self._services[name] = factory()
            return self._services[name]

        if name in self._services:
            callable_srv = self._services[name]
            if callable(callable_srv):
                return callable_srv()
            return callable_srv

        raise KeyError(f"Kernel Service not registered: {name}")

    def has(self, name: str) -> bool:
        """Check if a service is registered in any tier."""
        return name in self._singletons or name in self._factories or name in self._services

    # ------------------------------------------------------------------
    # Readiness — for background (Tier 1) services
    # ------------------------------------------------------------------

    def _event_for(self, name: str) -> asyncio.Event:
        event = self._ready_events.get(name)
        if event is None:
            event = asyncio.Event()
            self._ready_events[name] = event
        return event

    def mark_ready(self, name: str) -> None:
        """Signal that *name* is now registered and usable."""
        self._event_for(name).set()

    def is_ready(self, name: str) -> bool:
        """Return True if *name* is registered (synchronously usable now)."""
        if self.has(name):
            return True
        event = self._ready_events.get(name)
        return bool(event and event.is_set())

    async def wait_ready(self, name: str, timeout: float | None = None) -> bool:
        """Await until *name* is ready, or *timeout* elapses.

        Returns True if the service became ready, False on timeout. A service
        already registered resolves immediately.
        """
        if self.has(name):
            return True
        event = self._event_for(name)
        if timeout is None:
            await event.wait()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return self.has(name)

    def get_optional(self, name: str, default: Any = None) -> Any:
        """Return the service if registered, else *default* (never raises)."""
        try:
            return self.get(name) if self.has(name) else default
        except Exception:
            return default

    def clear(self) -> None:
        """Purge all registrations."""
        self._services.clear()
        self._factories.clear()
        self._singletons.clear()
        logger.debug("Kernel component registry cleared.")


# Global system container
_container = ServiceContainer()


def inject(service_name: str):
    """Decorator to inject a registered kernel component into function arguments."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if service_name not in kwargs:
                kwargs[service_name] = _container.get(service_name)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def get_container() -> ServiceContainer:
    """Access the global system service container."""
    return _container
