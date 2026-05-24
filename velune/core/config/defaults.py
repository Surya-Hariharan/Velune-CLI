"""Default configuration values."""

from velune.core.config.schema import (
    VeluneConfig,
    ProjectConfig,
    WorkspaceConfig,
    ContextConfig,
    MemoryConfig,
    RetrievalConfig,
    ExecutionConfig,
    ProvidersConfig,
    TelemetryConfig,
    MCPConfig,
)


def get_default_config() -> VeluneConfig:
    """Get default configuration."""
    return VeluneConfig(
        project=ProjectConfig(name="velune", version="0.1.0"),
        workspace=WorkspaceConfig(),
        context=ContextConfig(),
        memory=MemoryConfig(),
        retrieval=RetrievalConfig(),
        execution=ExecutionConfig(),
        providers=ProvidersConfig(),
        telemetry=TelemetryConfig(),
        mcp=MCPConfig(),
    )
