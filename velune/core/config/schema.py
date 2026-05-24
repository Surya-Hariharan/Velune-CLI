"""Configuration schema with validation."""

from typing import Any, Optional
from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    """Project-level configuration."""
    name: str
    version: str


class WorkspaceConfig(BaseModel):
    """Workspace cognition settings."""
    index_on_init: bool = True
    watch_files: bool = True
    git_aware: bool = True


class ContextConfig(BaseModel):
    """Context window management."""
    max_tokens: int = 128000
    compression_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    priority_tiers: list[str] = Field(default_factory=lambda: ["critical", "high", "medium", "low"])


class MemoryConfig(BaseModel):
    """Memory system configuration."""
    working_memory_ttl: int = 3600  # seconds
    episodic_retention_days: int = 30
    semantic_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    graph_enabled: bool = True


class RetrievalConfig(BaseModel):
    """Hybrid retrieval configuration."""
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    lexical_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    graph_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    rerank_top_k: int = Field(default=10, ge=1)


class ExecutionConfig(BaseModel):
    """Execution safety settings."""
    sandbox_enabled: bool = True
    auto_snapshot: bool = True
    require_confirmation: bool = True
    dry_run_default: bool = False


class ProviderEntry(BaseModel):
    """Individual provider configuration."""
    api_key_env: Optional[str] = None
    base_url: str


class ProvidersConfig(BaseModel):
    """Provider configuration."""
    default_provider: str = "openai"
    fallback_providers: list[str] = Field(default_factory=lambda: ["anthropic", "ollama"])
    openai: Optional[ProviderEntry] = None
    anthropic: Optional[ProviderEntry] = None
    ollama: Optional[ProviderEntry] = None
    lmstudio: Optional[ProviderEntry] = None


class TelemetryConfig(BaseModel):
    """Telemetry configuration."""
    enabled: bool = True
    export_otlp: bool = False
    log_level: str = "INFO"


class MCPConfig(BaseModel):
    """MCP configuration settings."""
    servers: dict[str, str] = Field(default_factory=dict)


class VeluneConfig(BaseModel):
    """Root Velune configuration."""
    project: ProjectConfig
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)

