"""Layered configuration engine with env overrides, schemas, and workspace discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import toml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass
class ConfigValidationError:
    """A single configuration validation problem."""
    field: str
    value: str | None
    reason: str
    severity: str = "CRITICAL"  # "CRITICAL" or "WARNING"


class ProjectConfig(BaseModel):
    """Project-level metadata."""
    name: str = "velune"
    version: str = "0.1.0"


class WorkspaceConfig(BaseModel):
    """Workspace cognition settings."""
    index_on_init: bool = True
    watch_files: bool = True
    git_aware: bool = True
    root: Path | None = None


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
    storage_dir: Path | None = None


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


class CognitionConfig(BaseModel):
    """Cognitive routing and agent council settings."""
    max_council_tier: str = "full"  # instant, minimal, standard, full
    default_tier_override: str = "auto"  # auto, instant, minimal, standard, full


class VeluneConfig(BaseSettings):
    """Root configuration tree.

    Field values are resolved in this priority order (highest first):
    1. Explicit constructor kwargs (e.g. from TOML file data)
    2. Environment variables prefixed with ``VELUNE_`` (nested via ``__``)
    3. A ``.env`` file in the working directory
    4. Hard-coded field defaults
    """

    model_config = SettingsConfigDict(
        env_prefix="VELUNE_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        env_ignore_empty=True,
    )

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    cognition: CognitionConfig = Field(default_factory=CognitionConfig)

    # ---------------------------------------------------------------------------
    # Startup validation
    # ---------------------------------------------------------------------------

    def validate(self) -> list[ConfigValidationError]:
        """Validate the configuration and return any problems found.

        Returns a list of :class:`ConfigValidationError`.  An empty list means
        the configuration is healthy.  Callers should treat ``severity="CRITICAL"``
        errors as startup-blocking failures.
        """
        errors: list[ConfigValidationError] = []

        provider_name = self.providers.default_provider
        provider_entry: ProviderEntry | None = getattr(self.providers, provider_name, None)

        if not isinstance(provider_entry, ProviderEntry):
            errors.append(ConfigValidationError(
                field="providers.default_provider",
                value=provider_name,
                reason=(
                    f"Provider '{provider_name}' is not defined in the [providers] section. "
                    f"Add a [{provider_name}] entry or change default_provider."
                ),
                severity="CRITICAL",
            ))
        else:
            # Remote providers require an API key env var to be populated.
            if provider_entry.api_key_env:
                resolved = os.getenv(provider_entry.api_key_env)
                if not resolved:
                    errors.append(ConfigValidationError(
                        field=f"providers.{provider_name}.api_key_env",
                        value=provider_entry.api_key_env,
                        reason=(
                            f"Environment variable '{provider_entry.api_key_env}' is not set. "
                            f"Provider '{provider_name}' requires an API key to function."
                        ),
                        severity="CRITICAL",
                    ))

        # Optional workspace root — validate if explicitly configured.
        if self.workspace.root is not None:
            wp = Path(self.workspace.root)
            if not wp.exists():
                errors.append(ConfigValidationError(
                    field="workspace.root",
                    value=str(wp),
                    reason=f"Workspace path '{wp}' does not exist.",
                    severity="CRITICAL",
                ))
            elif not os.access(wp, os.R_OK):
                errors.append(ConfigValidationError(
                    field="workspace.root",
                    value=str(wp),
                    reason=f"Workspace path '{wp}' exists but is not readable.",
                    severity="CRITICAL",
                ))

        # Optional memory storage directory — validate writability if configured.
        if self.memory.storage_dir is not None:
            sd = Path(self.memory.storage_dir)
            if not sd.exists():
                errors.append(ConfigValidationError(
                    field="memory.storage_dir",
                    value=str(sd),
                    reason=f"Storage directory '{sd}' does not exist.",
                    severity="WARNING",
                ))
            elif not os.access(sd, os.W_OK):
                errors.append(ConfigValidationError(
                    field="memory.storage_dir",
                    value=str(sd),
                    reason=f"Storage directory '{sd}' exists but is not writable.",
                    severity="CRITICAL",
                ))

        return errors

    def save_to_project(self, workspace: Path) -> Path:
        """Write only non-default values to .velune/config.toml.

        Returns the path the file was written to.
        """
        project_config_dir = workspace / ".velune"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        config_path = project_config_dir / "config.toml"

        current_data = self.model_dump()
        # Compare against hard-coded defaults, not an env-var-influenced instance.
        default_data = _hardcoded_defaults()
        non_default = _strip_defaults(current_data, default_data)

        with open(config_path, "w", encoding="utf-8") as f:
            toml.dump(non_default, f)

        return config_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hardcoded_defaults() -> dict:
    """Return a pure-default VeluneConfig dict, unaffected by environment variables."""
    instance = VeluneConfig.model_construct(
        project=ProjectConfig(),
        workspace=WorkspaceConfig(),
        context=ContextConfig(),
        memory=MemoryConfig(),
        retrieval=RetrievalConfig(),
        execution=ExecutionConfig(),
        providers=ProvidersConfig(),
        telemetry=TelemetryConfig(),
        mcp=MCPConfig(),
        cognition=CognitionConfig(),
    )
    return instance.model_dump()


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _strip_defaults(current: dict, defaults: dict) -> dict:
    """Return only entries in *current* that differ from *defaults*."""
    result: dict = {}
    for key, value in current.items():
        if key not in defaults:
            result[key] = value
        elif isinstance(value, dict) and isinstance(defaults[key], dict):
            stripped = _strip_defaults(value, defaults[key])
            if stripped:
                result[key] = stripped
        elif value != defaults[key]:
            result[key] = value
    return result


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
        """Parse the TOML config file if it exists, otherwise return defaults.

        Constructor kwargs take highest priority in BaseSettings, so TOML values
        override env vars when an explicit config file is present.  When no config
        file is found, ``VeluneConfig()`` is returned and env vars apply normally.
        """
        if not self.config_path or not self.config_path.exists():
            return get_default_config()

        try:
            data = toml.load(self.config_path)
            return VeluneConfig(**data)
        except Exception:
            return get_default_config()

    def load_with_env_overrides(self) -> VeluneConfig:
        """Load config from the TOML file (if present), with env var fallback.

        Since ``VeluneConfig`` is now a ``BaseSettings``, environment variables
        prefixed with ``VELUNE_`` are always consulted for any field not supplied
        by the TOML file.
        """
        return self.load()


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
