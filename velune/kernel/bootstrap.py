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
    """Declares a subsystem's factory and dependencies.

    ``tier`` controls *when* a module is initialized:

    * **0** — synchronous, on the critical path. Cheap, and required to render
      an interactive prompt or serve lightweight commands (``/model``,
      ``/help``). Bootstrapped before the prompt appears.
    * **1** — background warm-up. Expensive subsystems (memory tiers, vector
      stores, retrieval, repository cognition, orchestration) whose imports and
      factories run in a supervised background task *after* the prompt is
      interactive. Callers that need one ``await container.wait_ready(key)``.
    """

    name: str
    factory: Callable[["RuntimeEnvironment"], Any]
    container_key: str
    lifecycle_key: str | None = None  # None = not lifecycle-managed
    dependencies: list[str] = field(default_factory=list)  # container keys this needs
    tier: int = 1  # 0 = sync/instant, 1 = background warm


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
        """Initialize every registered module in dependency order.

        Modules with ``lifecycle_key`` set are lifecycle-critical (startup /
        shutdown managed by :class:`LifecycleCoordinator`); a factory failure
        aborts bootstrap. Modules with ``lifecycle_key=None`` are optional — a
        factory failure is logged and that module is skipped so the rest of the
        system still starts.
        """
        from velune.core.startup_profiler import mark

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
            env.container.mark_ready(module.container_key)
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


# ---------------------------------------------------------------------------
# Core primitives + hardware — split out of the module loop so the synchronous
# startup path stays tiny and the expensive hardware/GPU probe runs in the
# background warm-up instead of blocking the first prompt.
# ---------------------------------------------------------------------------


def register_core_primitives(env: RuntimeEnvironment) -> None:
    """Register the cheap, always-needed primitives synchronously.

    These are required by the REPL itself (background task supervision, the job
    registry and alert store the prompt/watcher read) and cost effectively
    nothing to construct, so they stay on the synchronous Tier-0 path.
    """
    from velune.core.task_registry import BackgroundTaskRegistry, JobRegistry
    from velune.proactive.alerts import AlertStore

    env.container.register_instance("runtime.task_registry", BackgroundTaskRegistry())
    env.container.register_instance("runtime.job_registry", JobRegistry())
    env.container.register_instance("runtime.alert_store", AlertStore())

    # A provisional hardware profile so the REPL (mode budgets, status bar)
    # constructs immediately. The accurate profile — including the ~0.5s GPU
    # probe — is computed in the background and hot-swapped in by
    # detect_hardware_profile().
    from velune.hardware.profiles import derive_profile
    from velune.hardware.quick import quick_hardware_profile

    provisional = quick_hardware_profile()
    env.container.register_instance("runtime.hardware", provisional)
    env.container.register_instance("runtime.profile", derive_profile(provisional))
    # Placeholder GPU info (consumers: council selection, `models scan`). The
    # real probe runs in detect_hardware_profile().
    env.container.register_instance(
        "runtime.gpu_info",
        {
            "has_gpu": bool(provisional.vram_total_gb),
            "gpu_type": provisional.gpu_name,
            "vram_total_gb": provisional.vram_total_gb,
            "vram_free_gb": None,
            "cuda_available": False,
        },
    )


def detect_hardware_profile(env: RuntimeEnvironment) -> None:
    """Run the full hardware/GPU probe and hot-swap the accurate profile in.

    Safe to call after :func:`register_core_primitives`; replaces the
    provisional profile registered there. Failures are non-fatal — the
    provisional profile remains in place.
    """
    from velune.core.startup_profiler import mark

    # Run the GPU probe first so HardwareDetector reuses its cache rather than
    # probing twice, and refresh the gpu_info consumers read.
    try:
        from velune.providers.discovery.gpu import GPUDetector

        gpu_info = GPUDetector().detect()
        env.container.hot_swap("runtime.gpu_info", gpu_info)
    except Exception as exc:
        _logger.warning("GPU detection failed, keeping placeholder gpu_info: %s", exc)

    try:
        from velune.hardware.detector import HardwareDetector
        from velune.hardware.profiles import derive_profile

        hardware_profile = HardwareDetector().detect()
        env.container.hot_swap("runtime.hardware", hardware_profile)
        env.container.hot_swap("runtime.profile", derive_profile(hardware_profile))
        mark("hardware detected (background)")
    except Exception as exc:  # provisional profile stays in place
        _logger.warning("Hardware detection failed, keeping provisional profile: %s", exc)
