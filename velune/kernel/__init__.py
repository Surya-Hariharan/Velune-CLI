"""Cognitive Kernel (the OS layer for Velune)."""

from velune.kernel.schemas import ComponentStatus, Event, HealthReport
from velune.kernel.bus import CognitiveBus, Subscription, EventBus, EventHandler
from velune.kernel.registry import ComponentRegistry, ServiceContainer, inject, get_container
from velune.kernel.lifecycle import LifecycleCoordinator, Subsystem
from velune.kernel.config import (
    VeluneConfig,
    ProjectConfig,
    WorkspaceConfig,
    ContextConfig,
    MemoryConfig,
    RetrievalConfig,
    ExecutionConfig,
    ProvidersConfig,
    ConfigLoader,
    ConfigService,
    get_default_config,
)
from velune.kernel.health import SubsystemHealthMonitor

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
