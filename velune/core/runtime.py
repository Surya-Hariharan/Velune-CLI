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
    # The bootstrap environment, retained so the interactive session can warm
    # the Tier-1 subsystems in the background once the prompt is interactive.
    # None for non-interactive command invocations that bootstrap everything
    # synchronously.
    env: object | None = None


def build_runtime(
    workspace: Path,
    config_path: Path | None = None,
    verbose: bool = False,
    *,
    defer_background: bool = False,
) -> RuntimeContext:
    """Create and register the shared runtime services.

    When ``defer_background`` is True only the Tier-0 (synchronous, instant)
    subsystems are bootstrapped — enough to render an interactive prompt and
    serve lightweight commands. The expensive Tier-1 subsystems (memory tiers,
    retrieval, repository cognition, orchestration) plus the hardware/GPU probe
    are left for :func:`warm_background` to initialize in a supervised task once
    the prompt is interactive. Non-interactive command invocations leave it
    False and bootstrap everything synchronously.
    """
    from velune.core.startup_profiler import mark

    mark("build_runtime: enter")
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
    mark("config loaded")

    # Applies before any Console/theme is built below so the very first frame
    # already reflects the configured palette, not just frames after a
    # `/theme colorblind` toggle.
    from velune.cli import design as _design

    _design.set_colorblind_mode(bool(getattr(config.display, "colorblind_mode", False)))
    _design.set_reduced_motion(bool(getattr(config.display, "reduced_motion", False)))

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

    # 3. Bootstrap subsystems. Tier-0 modules always run synchronously here.
    from velune.kernel.bootstrap import (
        RuntimeBootstrapper,
        RuntimeEnvironment,
        detect_hardware_profile,
        register_core_primitives,
    )
    from velune.kernel.modules import CORE_MODULES

    env = RuntimeEnvironment(
        workspace=workspace,
        config=config,
        container=container,
        lifecycle=lifecycle,
        verbose=verbose,
    )

    register_core_primitives(env)
    mark("core primitives registered")

    bootstrapper = RuntimeBootstrapper()
    for module in CORE_MODULES:
        bootstrapper.register_module(module)
    bootstrapper.bootstrap(env)
    mark("tier-0 subsystems bootstrapped")

    if not defer_background:
        # Non-interactive path: bring up hardware + every Tier-1 subsystem now
        # so the caller sees a fully-initialized container.
        detect_hardware_profile(env)
        bg = RuntimeBootstrapper()
        from velune.kernel.modules import load_background_modules

        for module in load_background_modules():
            bg.register_module(module)
        bg.bootstrap(env)
        mark("tier-1 subsystems bootstrapped")

    return RuntimeContext(
        workspace=workspace,
        config_path=config_path,
        config=config,
        console=console,
        logger_name=logger_name,
        container=container,
        env=env,
    )


async def warm_background(runtime: RuntimeContext) -> None:
    """Initialize the hardware probe and Tier-1 subsystems off the prompt path.

    Called from the interactive session's event loop after the prompt is drawn.
    Detects hardware/GPU, bootstraps every Tier-1 module, and runs lifecycle
    startup for the subsystems registered along the way. Best-effort: a failure
    in any optional subsystem is logged and skipped so the session stays usable.
    """
    import asyncio

    from velune.kernel.bootstrap import (
        RuntimeBootstrapper,
        detect_hardware_profile,
    )
    from velune.kernel.modules import load_background_modules

    env = runtime.env
    if env is None:
        return

    logger = logging.getLogger("velune")

    # Hardware/GPU probe (~0.5s) — run in a thread so it never blocks the loop.
    try:
        await asyncio.to_thread(detect_hardware_profile, env)
    except Exception as exc:
        logger.warning("Background hardware detection failed: %s", exc)

    # Importing + instantiating the Tier-1 modules is CPU/IO heavy; do it in a
    # worker thread, then register results back on the loop thread is
    # unnecessary because the container is plain dict access. Factories may do
    # blocking IO (SQLite, file reads), so a thread keeps the prompt responsive.
    def _bootstrap_tier1() -> None:
        bg = RuntimeBootstrapper()
        for module in load_background_modules():
            bg.register_module(module)
        bg.bootstrap(env)

    try:
        await asyncio.to_thread(_bootstrap_tier1)
    except Exception as exc:
        logger.warning("Background subsystem warm-up incomplete: %s", exc)

    # Mark the aggregate warm-up complete for any waiters.
    try:
        env.container.mark_ready("runtime.warm")
    except Exception:
        pass
