"""Default configuration values."""

from velune.kernel.config import (
    ContextConfig,
    ExecutionConfig,
    MCPConfig,
    MemoryConfig,
    ProjectConfig,
    ProvidersConfig,
    RetrievalConfig,
    TelemetryConfig,
    VeluneConfig,
    WorkspaceConfig,
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
