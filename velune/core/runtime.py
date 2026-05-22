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

# Stateful Orchestration & Memory Tiers
from velune.orchestration.engine import LangGraphOrchestrationEngine
from velune.memory.tiers.working import WorkingMemoryTier
from velune.memory.tiers.episodic import EpisodicMemoryTier
from velune.memory.tiers.semantic import SemanticMemoryTier
from velune.memory.tiers.graph import GraphMemoryTier
from velune.memory.tiers.archive import LongTermArchiveTier
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
    
    # Ensure local directory exists under .velune/
    velune_dir = workspace / ".velune"
    velune_dir.mkdir(parents=True, exist_ok=True)
    db_path = velune_dir / "velune_cognitive_core.db"
    vector_path = str(velune_dir / "qdrant_local_store")
    archive_dir = velune_dir / "archive"

    # Instantiate Tiers
    working_tier = WorkingMemoryTier()
    episodic_tier = EpisodicMemoryTier(db_path)
    semantic_tier = SemanticMemoryTier(path=vector_path)
    graph_tier = GraphMemoryTier(db_path)
    archive_tier = LongTermArchiveTier(archive_dir)

    # Instantiate Memory Coordinator & Consolidator
    consolidator = MemoryConsolidator(
        working_tier=working_tier,
        episodic_tier=episodic_tier,
        semantic_tier=semantic_tier,
        graph_tier=graph_tier,
        archive_tier=archive_tier,
    )
    memory_lifecycle = MemoryLifecycleCoordinator(consolidator)

    # Instantiate ToolRegistry & Register default tools
    tool_registry = ToolRegistry()
    default_tools = [
        ReadFile(), ReadDirectory(), WriteFile(), CreateFile(), DeleteFile(),
        GrepFiles(), FindFiles(), GitLog(), GitDiff(), GitBlame(), GitStatus(), GitBranch(),
        GitCommit(), GitCheckout(), ExecuteCommand(), TerminalHistory(),
        SemanticCodeSearch(), SymbolSearch(), GoToDefinition(), FindReferences(), WebFetch()
    ]
    for tool in default_tools:
        tool_registry.register(tool)

    # Embedded/in-process Qdrant points to local store
    hybrid_retriever = HybridRetriever(location=vector_path, client=semantic_tier.client)
    
    # Sandbox execution limits CPU/Memory resources on Windows
    execution_executor = ExecutionExecutor(workspace)
    
    # Cognitive Firewall
    cognitive_firewall = CognitiveFirewall()
    
    # LangGraph deliberative multi-agent reasoning council
    council_orchestrator = CouncilOrchestrator(provider_registry, model_specialization)

    # LangGraph stateful orchestration engine
    orchestration_engine = LangGraphOrchestrationEngine(
        retrieval=hybrid_retriever,
        repository_cognition=repository_cognition,
        memory_lifecycle=memory_lifecycle,
        graph_memory=graph_tier,
        tool_registry=tool_registry,
    )

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
    container.register_instance("runtime.orchestration_engine", orchestration_engine)
    container.register_instance("runtime.firewall", cognitive_firewall)
    container.register_instance("runtime.workspace", workspace)
    container.register_instance("runtime.config_path", config_path)
    
    # Memory Tier registrations
    container.register_instance("runtime.working_memory", working_tier)
    container.register_instance("runtime.episodic_memory", episodic_tier)
    container.register_instance("runtime.semantic_memory", semantic_tier)
    container.register_instance("runtime.graph_memory", graph_tier)
    container.register_instance("runtime.archive_memory", archive_tier)
    container.register_instance("runtime.memory_lifecycle", memory_lifecycle)
    container.register_instance("runtime.memory_consolidator", consolidator)
    container.register_instance("runtime.tool_registry", tool_registry)

    # 5. Register Subsystems in Lifecycle Coordinator
    lifecycle.register("bus", bus)
    lifecycle.register("providers", provider_registry)
    lifecycle.register("models", model_registry)
    lifecycle.register("repository", repository_cognition)
    lifecycle.register("retrieval", hybrid_retriever)
    lifecycle.register("execution", execution_executor)
    lifecycle.register("memory", memory_lifecycle)

    return RuntimeContext(
        workspace=workspace,
        config_path=config_path,
        config=config,
        console=console,
        logger_name=logger_name,
        container=container,
    )