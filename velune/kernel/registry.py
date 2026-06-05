"""Kernel component registry — string-keyed ServiceContainer with lazy factories."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger("velune.kernel.registry")

T = TypeVar("T")


class ServiceContainer:
    """Component registry container supporting string-based lazy factories."""

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._factories: dict[str, Callable[[], Any]] = {}
        self._singletons: dict[str, Any] = {}

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
