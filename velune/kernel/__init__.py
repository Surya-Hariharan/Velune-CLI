"""Cognitive Kernel (the OS layer for Velune)."""

from velune.kernel.bus import CognitiveBus, EventBus, EventHandler, Subscription
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
from velune.kernel.registry import ComponentRegistry, ServiceContainer, get_container, inject
from velune.kernel.schemas import ComponentStatus, Event, HealthReport

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
    "ComponentRegistry",
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
