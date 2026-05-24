"""Process runtime bootstrap for Velune Cognitive OS CLI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from velune.kernel.config import ConfigService, get_default_config, VeluneConfig
from velune.kernel.bus import CognitiveBus
from velune.kernel.lifecycle import LifecycleCoordinator
from velune.providers.registry import ProviderRegistry
from velune.models.registry import ModelCapabilityRegistry
from velune.models.specializations import ModelSpecializationMapper
from velune.repository.cognition import RepositoryCognitionService
from velune.retrieval.hybrid import HybridRetriever
from velune.execution.executor import ExecutionExecutor
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.kernel.registry import ServiceContainer

# Stateful Orchestration & Memory Tiers
from velune.orchestration.engine import LangGraphOrchestrationEngine
from velune.memory.tiers.working import WorkingMemoryTier
from velune.memory.tiers.episodic import EpisodicMemoryTier
from velune.memory.tiers.semantic import SemanticMemoryTier
from velune.memory.tiers.graph import GraphMemoryTier
from velune.memory.tiers.archive import LongTermArchiveTier
from velune.memory.storage.sqlite_manager import SQLiteManager
from velune.memory.consolidator import MemoryConsolidator
from velune.memory.lifecycle import MemoryLifecycleCoordinator
from velune.cognition.firewall import CognitiveFirewall
from velune.tools.base.registry import ToolRegistry
from velune.tools import (
    ReadFile, ReadDirectory, WriteFile, CreateFile, DeleteFile,
    GrepFiles, FindFiles, GitLog, GitDiff, GitBlame, GitStatus, GitBranch,
    GitCommit, GitCheckout, ExecuteCommand, TerminalHistory,
    SemanticCodeSearch, SymbolSearch, GoToDefinition, FindReferences, WebFetch
)


@dataclass(slots=True)
class RuntimeContext:
    """Runtime resources shared across CLI commands."""

    workspace: Path
    config_path: Optional[Path]
    config: VeluneConfig
    console: Console
    logger_name: str
    container: ServiceContainer


def build_runtime(
    workspace: Path,
    config_path: Optional[Path] = None,
    verbose: bool = False,
) -> RuntimeContext:
    """Create and register the shared runtime services using the declarative bootstrapper."""
    # 1. Setup Logging and Console
    console = Console(highlight=False)
    logger_name = "velune"
    logger = logging.getLogger(logger_name)
    
    # Configure root logger levels
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # 2. Configuration Service
    config_service = ConfigService(workspace=workspace, config_path=config_path)
    try:
        config = config_service.load()
    except Exception as e:
        logger.warning("Failed to load configuration. Falling back to system defaults: %s", e)
        config = get_default_config()

    container = ServiceContainer()
    lifecycle = LifecycleCoordinator()

    # Pre-register basic primitive context dependencies
    container.register_instance("runtime.config", config)
    container.register_instance("runtime.config_service", config_service)
    container.register_instance("runtime.console", console)
    container.register_instance("runtime.logger", logger)
    container.register_instance("runtime.workspace", workspace)
    container.register_instance("runtime.config_path", config_path)

    # Detect and persist GPU info
    from velune.providers.discovery.gpu import GPUDetector
    gpu_info = GPUDetector().detect()
    container.register_instance("runtime.gpu_info", gpu_info)

    # 3. Initialize and bootstrap declarative subsystems in dependency order
    from velune.kernel.bootstrap import RuntimeEnvironment, RuntimeBootstrapper
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