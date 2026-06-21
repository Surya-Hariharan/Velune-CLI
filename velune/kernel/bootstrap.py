import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.kernel.config import VeluneConfig
from velune.kernel.lifecycle import LifecycleCoordinator
from velune.kernel.registry import ServiceContainer

_logger = logging.getLogger(__name__)


@dataclass
class SubsystemModule:
    """Declares a subsystem's factory and dependencies."""

    name: str
    factory: Callable[["RuntimeEnvironment"], Any]
    container_key: str
    lifecycle_key: str | None = None  # None = not lifecycle-managed
    dependencies: list[str] = field(default_factory=list)  # container keys this needs


@dataclass
class RuntimeEnvironment:
    """Everything a subsystem factory needs to initialize itself."""

    workspace: Path
    config: VeluneConfig
    container: ServiceContainer
    lifecycle: LifecycleCoordinator
    verbose: bool = False


class RuntimeBootstrapper:
    def __init__(self) -> None:
        self._modules: list[SubsystemModule] = []

    def register_module(self, module: SubsystemModule) -> None:
        self._modules.append(module)

    def bootstrap(self, env: RuntimeEnvironment) -> None:
        """Initialize all modules in dependency order."""
        from velune.core.startup_profiler import mark
        from velune.core.task_registry import BackgroundTaskRegistry

        registry = BackgroundTaskRegistry()
        env.container.register_instance("runtime.task_registry", registry)

        from velune.core.task_registry import JobRegistry

        job_registry = JobRegistry()
        env.container.register_instance("runtime.job_registry", job_registry)

        from velune.proactive.alerts import AlertStore

        alert_store = AlertStore()
        env.container.register_instance("runtime.alert_store", alert_store)

        from velune.hardware.detector import HardwareDetector
        from velune.hardware.profiles import derive_profile

        hardware_profile = HardwareDetector().detect()
        env.container.register_instance("runtime.hardware", hardware_profile)
        env.container.register_instance("runtime.profile", derive_profile(hardware_profile))
        mark("hardware detected")

        # Modules with lifecycle_key set are lifecycle-critical (startup/shutdown
        # managed by LifecycleCoordinator). A factory failure aborts bootstrap.
        # Modules with lifecycle_key=None are optional; a factory failure is
        # logged and that module is skipped, letting the rest of the system start.
        resolved = self._topological_sort()
        for module in resolved:
            try:
                instance = module.factory(env)
                mark(f"module: {module.name}")
            except Exception as exc:
                if module.lifecycle_key:
                    _logger.critical(
                        "Critical module '%s' (%s) failed to initialize: %s",
                        module.name,
                        module.container_key,
                        exc,
                    )
                    raise
                _logger.warning(
                    "Optional module '%s' (%s) failed to initialize, skipping: %s",
                    module.name,
                    module.container_key,
                    exc,
                )
                continue
            env.container.register_instance(module.container_key, instance)
            if module.lifecycle_key:
                env.lifecycle.register(module.lifecycle_key, instance)

    def _topological_sort(self) -> list[SubsystemModule]:
        # Kahn's algorithm on dependency graph
        in_degree = {}
        graph = {}
        key_to_mod = {mod.container_key: mod for mod in self._modules}

        for mod in self._modules:
            in_degree[mod.container_key] = 0
            graph[mod.container_key] = []

        for mod in self._modules:
            # only consider dependencies that are other modules
            for dep in mod.dependencies:
                if dep in key_to_mod:
                    graph[dep].append(mod.container_key)
                    in_degree[mod.container_key] += 1

        # Find all modules with in_degree == 0
        from collections import deque

        queue = deque(
            [mod.container_key for mod in self._modules if in_degree[mod.container_key] == 0]
        )

        resolved_keys = []
        while queue:
            node = queue.popleft()
            resolved_keys.append(node)
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(resolved_keys) != len(self._modules):
            unresolved = set(key_to_mod.keys()) - set(resolved_keys)
            raise ValueError(
                f"Circular dependency or missing module dependency detected in bootstrap! Unresolved: {unresolved}"
            )

        return [key_to_mod[key] for key in resolved_keys]
