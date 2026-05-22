"""Process runtime bootstrap for Velune CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from velune.core.config.loader import ConfigLoader
from velune.core.config.defaults import get_default_config
from velune.core.config.service import ConfigService
from velune.core.config.schema import VeluneConfig
from velune.core.logging import LoggingConfig, configure_logging, get_logger
from velune.core.registry.container import ServiceContainer
from velune.models.discovery import ModelDiscoveryService, ModelRegistry
from velune.memory.graph.service import GraphMemoryService
from velune.memory.lifecycle.service import MemoryLifecycleService
from velune.repository.cognition.service import RepositoryCognitionService
from velune.retrieval.hybrid.service import HybridRetrievalEngine
from velune.retrieval.lexical.memory import InMemoryLexicalIndex
from velune.retrieval.vector.memory import InMemoryVectorStore
from velune.providers.registry import ProviderRegistry


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

    try:
        loader = ConfigLoader(config_path=config_path)
        config = loader.load_with_env_overrides()
    except FileNotFoundError:
        config = get_default_config()

    console = Console(highlight=False)
    logging_config = LoggingConfig(level="DEBUG" if verbose else config.telemetry.log_level)
    configure_logging(logging_config)

    container = ServiceContainer()
    logger_name = "velune"
    logger = get_logger(logger_name)

    provider_registry = ProviderRegistry(config.providers)
    model_registry = ModelRegistry()
    model_discovery = ModelDiscoveryService(registry=model_registry, workspace=workspace)
    config_service = ConfigService(workspace=workspace, config_path=config_path)
    vector_store = InMemoryVectorStore()
    lexical_index = InMemoryLexicalIndex()
    hybrid_retrieval = HybridRetrievalEngine(vector_store=vector_store, lexical_index=lexical_index)
    graph_memory = GraphMemoryService()
    memory_lifecycle = MemoryLifecycleService()
    repository_cognition = RepositoryCognitionService()

    container.register_instance("runtime.config", config)
    container.register_instance("runtime.config_service", config_service)
    container.register_instance("runtime.console", console)
    container.register_instance("runtime.logger", logger)
    container.register_instance("runtime.provider_registry", provider_registry)
    container.register_instance("runtime.model_registry", model_registry)
    container.register_instance("runtime.model_discovery", model_discovery)
    container.register_instance("runtime.vector_store", vector_store)
    container.register_instance("runtime.lexical_index", lexical_index)
    container.register_instance("runtime.retrieval", hybrid_retrieval)
    container.register_instance("runtime.graph_memory", graph_memory)
    container.register_instance("runtime.memory_lifecycle", memory_lifecycle)
    container.register_instance("runtime.repository_cognition", repository_cognition)
    container.register_instance("runtime.workspace", workspace)
    container.register_instance("runtime.config_path", config_path)

    return RuntimeContext(
        workspace=workspace,
        config_path=config_path,
        config=config,
        console=console,
        logger_name=logger_name,
        container=container,
    )