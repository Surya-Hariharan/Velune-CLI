"""Process runtime bootstrap for Velune Cognitive OS CLI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from velune.kernel.config import ConfigService, VeluneConfig, get_default_config
from velune.kernel.lifecycle import LifecycleCoordinator
from velune.kernel.registry import ServiceContainer

# Stateful Orchestration & Memory Tiers


@dataclass(slots=True)
class RuntimeContext:
    """Runtime resources shared across CLI commands."""

    workspace: Path
    config_path: Path | None
    config: VeluneConfig
    console: Console
    logger_name: str
    container: ServiceContainer


def build_runtime(
    workspace: Path,
    config_path: Path | None = None,
    verbose: bool = False,
) -> RuntimeContext:
    """Create and register the shared runtime services using the declarative bootstrapper."""
    # 1. Setup Logging and Console
    console = Console(highlight=False)
    logger_name = "velune"
    logger = logging.getLogger(logger_name)

    # Configure root logger levels. Non-verbose hides internal INFO/DEBUG
    # logs so users only ever see Rich-formatted output, not raw Python logs.
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)

    # 2. Configuration Service
    config_service = ConfigService(workspace=workspace, config_path=config_path)
    try:
        config = config_service.load()
    except Exception as e:
        logger.warning("Failed to load configuration. Falling back to system defaults: %s", e)
        config = get_default_config()

    container = ServiceContainer()
    lifecycle = LifecycleCoordinator()
    lifecycle.container = container

    # Pre-register basic primitive context dependencies
    container.register_instance("runtime.config", config)
    container.register_instance("runtime.config_service", config_service)
    container.register_instance("runtime.console", console)
    container.register_instance("runtime.logger", logger)
    container.register_instance("runtime.workspace", workspace)
    container.register_instance("runtime.config_path", config_path)
    container.register_instance("runtime.lifecycle", lifecycle)

    # Detect and persist GPU info
    try:
        from velune.providers.discovery.gpu import GPUDetector
        gpu_info = GPUDetector().detect()
    except Exception as e:
        logger.warning("GPU detection failed, using safe defaults: %s", e)
        gpu_info = {
            "has_gpu": False,
            "gpu_type": None,
            "vram_total_gb": None,
            "vram_free_gb": None,
            "cuda_available": False,
        }
    container.register_instance("runtime.gpu_info", gpu_info)

    # 3. Initialize and bootstrap declarative subsystems in dependency order
    from velune.kernel.bootstrap import RuntimeBootstrapper, RuntimeEnvironment
    from velune.kernel.modules import ALL_MODULES

    env = RuntimeEnvironment(
        workspace=workspace,
        config=config,
        container=container,
        lifecycle=lifecycle,
        verbose=verbose,
    )

    bootstrapper = RuntimeBootstrapper()
    for module in ALL_MODULES:
        bootstrapper.register_module(module)
    bootstrapper.bootstrap(env)

    return RuntimeContext(
        workspace=workspace,
        config_path=config_path,
        config=config,
        console=console,
        logger_name=logger_name,
        container=container,
    )
