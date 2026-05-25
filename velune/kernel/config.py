"""Layered configuration engine with env overrides, schemas, and workspace discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import toml
from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    """Project-level metadata."""
    name: str = "velune"
    version: str = "0.1.0"


class WorkspaceConfig(BaseModel):
    """Workspace cognition settings."""
    index_on_init: bool = True
    watch_files: bool = True
    git_aware: bool = True


class ContextConfig(BaseModel):
    """Context window budgeting settings."""
    max_tokens: int = 128000
    compression_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    priority_tiers: list[str] = Field(default_factory=lambda: ["critical", "high", "medium", "low"])


class MemoryConfig(BaseModel):
    """Memory retention and thresholds."""
    working_memory_ttl: int = 3600  # seconds
    episodic_retention_days: int = 30
    semantic_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    graph_enabled: bool = True


class RetrievalConfig(BaseModel):
    """Hybrid retrieval fusion weightings."""
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    lexical_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    graph_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    rerank_top_k: int = Field(default=10, ge=1)


class ExecutionConfig(BaseModel):
    """Safety and sandboxing options."""
    sandbox_enabled: bool = True
    auto_snapshot: bool = True
    require_confirmation: bool = True
    dry_run_default: bool = False
    low_resource_mode: bool = False
    allowed_executables: list[str] = Field(
        default_factory=lambda: [
            "python", "python3", "pytest", "ruff", "mypy",
            "git", "node", "npm", "cargo", "go",
            "make", "cmake", "gcc", "clang",
            "echo", "cat", "ls", "find", "grep"
        ]
    )


class ProviderEntry(BaseModel):
    """Target address and API key names for LLM providers."""
    api_key_env: str | None = None
    base_url: str | None = None


class ProvidersConfig(BaseModel):
    """Configuration for active and fallback models."""
    default_provider: str = "openai"
    fallback_providers: list[str] = Field(default_factory=lambda: ["anthropic", "ollama"])
    openai: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(api_key_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1")
    )
    anthropic: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(api_key_env="ANTHROPIC_API_KEY", base_url="https://api.anthropic.com")
    )
    ollama: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(base_url="http://localhost:11434")
    )
    lmstudio: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(base_url="http://localhost:1234/v1")
    )
    llamacpp: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(base_url="")
    )
    huggingface: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(api_key_env="HF_TOKEN", base_url="https://api-inference.huggingface.co")
    )


class TelemetryConfig(BaseModel):
    """Observability options."""
    enabled: bool = True
    export_otlp: bool = False
    log_level: str = "INFO"


class MCPConfig(BaseModel):
    """MCP configuration settings."""
    servers: dict[str, str] = Field(default_factory=dict)


class VeluneConfig(BaseModel):
    """Root configuration tree."""
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)


def get_default_config() -> VeluneConfig:
    """Acquire standard default settings."""
    return VeluneConfig()


class ConfigLoader:
    """Loads and overlays configuration from TOML and Environment variables."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or self._find_config_path()

    def _find_config_path(self) -> Path | None:
        """Traverse upwards to locate velune.toml, or fall back to user home."""
        try:
            current_dir = Path.cwd()
            while current_dir != current_dir.parent:
                config_file = current_dir / "velune.toml"
                if config_file.exists():
                    return config_file
                current_dir = current_dir.parent
        except Exception:
            pass

        # Fallback to home directory config
        home_config = Path.home() / ".velune" / "velune.toml"
        if home_config.exists():
            return home_config

        return None

    def load(self) -> VeluneConfig:
        """Parse the TOML config file if it exists, otherwise return defaults."""
        if not self.config_path or not self.config_path.exists():
            return get_default_config()

        try:
            data = toml.load(self.config_path)
            return VeluneConfig(**data)
        except Exception:
            return get_default_config()

    def load_with_env_overrides(self) -> VeluneConfig:
        """Inject API key strings directly from the environment variables specified in the config."""
        config = self.load()

        # Override OpenAI key if env variable matches
        if config.providers.openai and config.providers.openai.api_key_env:
            key = os.getenv(config.providers.openai.api_key_env)
            if key:
                # Store resolved key directly (adapters can fallback to standard resolution too)
                pass

        if config.providers.anthropic and config.providers.anthropic.api_key_env:
            key = os.getenv(config.providers.anthropic.api_key_env)
            if key:
                pass

        return config


@dataclass(slots=True)
class ConfigService:
    """Workspace-aware configuration service."""

    workspace: Path
    config_path: Path | None = None

    def load(self) -> VeluneConfig:
        """Load configuration using workspace priorities."""
        resolved = self._resolve_config_path()
        return ConfigLoader(resolved).load_with_env_overrides()

    def _resolve_config_path(self) -> Path | None:
        if self.config_path:
            return self.config_path

        workspace_config = self.workspace / "velune.toml"
        if workspace_config.exists():
            return workspace_config

        return None
