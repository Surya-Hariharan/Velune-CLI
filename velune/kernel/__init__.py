"""Cognitive Kernel (the OS layer for Velune)."""

from velune.events import CognitiveBus, Event, EventBus, EventHandler, Subscription
from velune.kernel.config import (
    ConfigLoader,
    ConfigService,
    ContextConfig,
    ExecutionConfig,
    MemoryConfig,
    ProjectConfig,
    ProvidersConfig,
    RetrievalConfig,
    VeluneConfig,
    WorkspaceConfig,
    get_default_config,
)
from velune.kernel.health import SubsystemHealthMonitor
from velune.kernel.lifecycle import LifecycleCoordinator, Subsystem
from velune.kernel.registry import ServiceContainer, get_container, inject
from velune.kernel.schemas import ComponentStatus, HealthReport

__all__ = [
    # Schemas
    "ComponentStatus",
    "Event",
    "HealthReport",

    # Bus
    "CognitiveBus",
    "Subscription",
    "EventBus",
    "EventHandler",

    # Registry
    "ServiceContainer",
    "inject",
    "get_container",

    # Lifecycle
    "LifecycleCoordinator",
    "Subsystem",

    # Config
    "VeluneConfig",
    "ProjectConfig",
    "WorkspaceConfig",
    "ContextConfig",
    "MemoryConfig",
    "RetrievalConfig",
    "ExecutionConfig",
    "ProvidersConfig",
    "ConfigLoader",
    "ConfigService",
    "get_default_config",

    # Health
    "SubsystemHealthMonitor",
]
