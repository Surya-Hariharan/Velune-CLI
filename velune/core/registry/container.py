"""Dependency injection container."""

from typing import Any, Callable, Dict, Optional, TypeVar
from functools import wraps


T = TypeVar("T")


class ServiceContainer:
    """Simple dependency injection container."""

    def __init__(self):
        self._services: Dict[str, Any] = {}
        self._factories: Dict[str, Callable[[], Any]] = {}
        self._singletons: Dict[str, Any] = {}

    def register(self, name: str, factory: Callable[[], T], singleton: bool = True) -> None:
        """Register a service factory."""
        if singleton:
            self._factories[name] = factory
        else:
            self._services[name] = factory

    def register_instance(self, name: str, instance: Any) -> None:
        """Register a service instance."""
        self._singletons[name] = instance

    def get(self, name: str) -> Any:
        """Get a service instance."""
        # Check singletons first
        if name in self._singletons:
            return self._singletons[name]
        
        # Check factories
        if name in self._factories:
            if name not in self._services:
                self._services[name] = self._factories[name]()
            return self._services[name]
        
        # Check non-singleton services
        if name in self._services:
            if callable(self._services[name]):
                return self._services[name]()
            return self._services[name]
        
        raise KeyError(f"Service not registered: {name}")

    def has(self, name: str) -> bool:
        """Check if a service is registered."""
        return name in self._singletons or name in self._factories or name in self._services

    def clear(self) -> None:
        """Clear all services."""
        self._services.clear()
        self._factories.clear()
        self._singletons.clear()


# Global container instance
_container = ServiceContainer()


def inject(service_name: str):
    """Decorator for dependency injection."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if service_name not in kwargs:
                kwargs[service_name] = _container.get(service_name)
            return func(*args, **kwargs)
        return wrapper
    return decorator


def get_container() -> ServiceContainer:
    """Get the global service container."""
    return _container
