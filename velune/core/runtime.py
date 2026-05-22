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
from velune.core.registry.container import ServiceContainer


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
    """Create and register the shared runtime services."""

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

    # 3. Instantiate Cognitive OS Subsystems
    bus = CognitiveBus()
    lifecycle = LifecycleCoordinator()
    provider_registry = ProviderRegistry(config.providers)
    model_registry = ModelCapabilityRegistry()
    model_specialization = ModelSpecializationMapper(model_registry)
    repository_cognition = RepositoryCognitionService(workspace)
    
    # Embedded/in-process Qdrant falls back nicely
    hybrid_retriever = HybridRetriever(location=":memory:")
    
    # Sandbox execution limits CPU/Memory resources on Windows
    execution_executor = ExecutionExecutor(workspace)
    
    # LangGraph deliberative multi-agent reasoning council
    council_orchestrator = CouncilOrchestrator(provider_registry, model_specialization)

    # 4. Populate Dependency Injection Container
    container = ServiceContainer()
    container.register_instance("runtime.config", config)
    container.register_instance("runtime.config_service", config_service)
    container.register_instance("runtime.console", console)
    container.register_instance("runtime.logger", logger)
    container.register_instance("runtime.bus", bus)
    container.register_instance("runtime.lifecycle", lifecycle)
    container.register_instance("runtime.provider_registry", provider_registry)
    container.register_instance("runtime.model_registry", model_registry)
    container.register_instance("runtime.model_discovery", model_registry.scanner)  # Backward compat for commands.models
    container.register_instance("runtime.repository_cognition", repository_cognition)
    container.register_instance("runtime.retrieval", hybrid_retriever)
    container.register_instance("runtime.execution_executor", execution_executor)
    container.register_instance("runtime.council_orchestrator", council_orchestrator)
    container.register_instance("runtime.workspace", workspace)
    container.register_instance("runtime.config_path", config_path)

    # 5. Register Subsystems in Lifecycle Coordinator
    lifecycle.register("bus", bus)
    lifecycle.register("providers", provider_registry)
    lifecycle.register("models", model_registry)
    lifecycle.register("repository", repository_cognition)
    lifecycle.register("retrieval", hybrid_retriever)
    lifecycle.register("execution", execution_executor)

    return RuntimeContext(
        workspace=workspace,
        config_path=config_path,
        config=config,
        console=console,
        logger_name=logger_name,
        container=container,
    )