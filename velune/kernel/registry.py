"""Kernel component registry with hot-swap support."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger("velune.kernel.registry")

T = TypeVar("T")


class ComponentRegistry:
    """Type-safe dynamic component registry supporting hot-swappable interfaces."""

    def __init__(self) -> None:
        self._registry: dict[type[Any], Any] = {}

    def register(self, interface: type[T], implementation: T) -> None:
        """Register a concrete implementation for a given interface Type."""
        self._registry[interface] = implementation
        logger.debug("Registered component for interface: %s", interface.__name__ if hasattr(interface, "__name__") else str(interface))

    def get(self, interface: type[T]) -> T:
        """Retrieve the registered implementation for an interface Type."""
        if interface in self._registry:
            return self._registry[interface]
        interface_name = interface.__name__ if hasattr(interface, "__name__") else str(interface)
        raise KeyError(f"Kernel component not registered for interface: {interface_name}")

    def swap(self, interface: type[T], new_impl: T) -> None:
        """Hot-swap the implementation of an active interface in-flight."""
        logger.info("Hot-swapping interface %s implementation.", interface.__name__ if hasattr(interface, "__name__") else str(interface))
        self._registry[interface] = new_impl


class ServiceContainer:
    """Backward-compatible component registry container supporting string-based lazy factories."""

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._factories: dict[str, Callable[[], Any]] = {}
        self._singletons: dict[str, Any] = {}
        self._type_registry = ComponentRegistry()

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
        if name in self._singletons:
            logger.info("Hot-swapping concrete service instance: '%s'", name)
            self._singletons[name] = replacement
        elif name in self._factories:
            logger.info("Hot-swapping singleton factory: '%s'", name)
            self._factories[name] = lambda: replacement
            if name in self._services:
                self._services[name] = replacement
        else:
            logger.info("Registering hot-swapped service from scratch: '%s'", name)
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
