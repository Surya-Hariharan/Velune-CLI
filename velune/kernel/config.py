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
    # Per-workspace default for `/hunk` (hunk-by-hunk diff review before
    # applying edits). Persisted to this workspace's velune.toml by the
    # `/hunk` command itself, so toggling it once carries over to the next
    # session in the same workspace instead of resetting every launch.
    hunk_review_default: bool = False
    low_resource_mode: bool = False
    # Docker sandbox — when True, all agent-executed commands run inside a
    # per-session Docker container instead of the host subprocess sandbox.
    # Requires Docker Desktop / Docker Engine to be running.
    docker_sandbox: bool = False
    docker_image: str = "python:3.12-slim"
    # Native tool calling in chat — when True (default), models that support
    # function calling can invoke workspace tools mid-conversation through the
    # permission-gated tool loop. Read-only tools run without prompting;
    # write/exec tools follow the session approval mode (/approve). Disable
    # with VELUNE_EXECUTION__NATIVE_TOOLS=0 or [execution] native_tools=false.
    native_tools: bool = True
    # Upper bound on model turns per prompt inside the tool loop.
    max_tool_turns: int = Field(default=10, ge=1, le=50)
    allowed_executables: list[str] = Field(
        default_factory=lambda: [
            "python",
            "python3",
            "pytest",
            "ruff",
            "mypy",
            "git",
            "node",
            "npm",
            "cargo",
            "go",
            "make",
            "cmake",
            "gcc",
            "clang",
            "echo",
            "cat",
            "ls",
            "find",
            "grep",
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
    cost_threshold_usd: float = Field(
        default=0.01,
        description="Prompt for confirmation before cloud calls estimated to cost more than this (USD). Set to 0 to always ask.",
    )
    max_concurrent_requests: int = Field(
        default=4,
        ge=1,
        description=(
            "Max in-flight infer()/stream() calls per provider at once (e.g. "
            "council agents fanning out concurrently). Extra callers queue "
            "rather than firing all at once — protects against tripping a "
            "provider's own per-key rate limit under concurrent load."
        ),
    )
    openai: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(
            api_key_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1"
        )
    )
    anthropic: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(
            api_key_env="ANTHROPIC_API_KEY", base_url="https://api.anthropic.com"
        )
    )
    ollama: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(base_url="http://localhost:11434")
    )
    lmstudio: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(base_url="http://localhost:1234/v1")
    )
    # Generic OpenAI-compatible local server (vLLM, LocalAI, llama.cpp server, …).
    openai_compat: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(base_url="http://localhost:8000/v1")
    )
    llamacpp: ProviderEntry | None = Field(default_factory=lambda: ProviderEntry(base_url=""))
    huggingface: ProviderEntry | None = Field(
        default_factory=lambda: ProviderEntry(
            api_key_env="HF_TOKEN", base_url="https://api-inference.huggingface.co"
        )
    )


class DisplayConfig(BaseModel):
    """Terminal UI layout options for the fullscreen REPL."""

    # Caps how wide the REPL's content column (conversation, borders, banner)
    # ever renders, regardless of actual terminal width — see the comment on
    # `_MAX_CONTENT_WIDTH` in velune/cli/fullscreen.py for why 100 is the
    # default. A wider terminal just grows empty side gutters instead of
    # reflowing content; set this higher to use the full window width, or
    # lower for a narrower reading column.
    content_max_width: int = Field(default=100, ge=20)
    # Swaps the OK/WARN/DANGER severity colors for a colorblind-safe
    # (Okabe-Ito) alternate palette — see `set_colorblind_mode()` in
    # velune/cli/design.py. Toggle live with `/theme colorblind`.
    colorblind_mode: bool = False
    # Freezes the thinking/tool-card spinners to a static frame instead of
    # animating them — see `set_reduced_motion()` in velune/cli/design.py.
    # Toggle live with `/theme motion`, or set VELUNE_REDUCED_MOTION=1.
    reduced_motion: bool = False


class TelemetryConfig(BaseModel):
    """Observability options."""

    enabled: bool = True
    export_otlp: bool = False
    log_level: str = "INFO"
    # Off by default — Velune is "zero telemetry" (see SECURITY.md): nothing
    # is written and nothing is ever transmitted anywhere unless a user
    # explicitly opts in. When True, an unhandled crash gets a redacted JSON
    # snapshot (exception, traceback, versions — no local variables, no
    # prompts/conversation content) written to ~/.velune/crash_reports/ for
    # the user's own diagnosis; Velune has no server to send it to. See
    # velune/cli/crash_reporter.py. Toggle with `/crashreports on`.
    crash_reports_enabled: bool = False


class MCPConfig(BaseModel):
    """MCP configuration settings."""

    servers: dict[str, str] = Field(default_factory=dict)
    #: Optional allowlist of permitted external MCP hostnames. When non-empty,
    #: the client refuses to connect to any host not listed here (deny-by-default).
    #: Empty means "no allowlist" — cloud-metadata/link-local targets are still
    #: always blocked by the SSRF guard regardless of this setting.
    allowed_hosts: list[str] = Field(default_factory=list)


class CognitionConfig(BaseModel):
    """Cognitive routing and agent council settings."""

    max_council_tier: str = "full"  # instant, minimal, standard, full
    default_tier_override: str = "auto"  # auto, instant, minimal, standard, full


class ResourceEntry(BaseModel):
    """Per-connector settings for the Resource Connector Framework."""

    #: When False the connector is hidden from discovery, status, and execution.
    enabled: bool = True
    #: When True the REPL connects the resource at startup (if it's available).
    auto_connect: bool = False


class ResourcesConfig(BaseModel):
    """Configuration for external resource connectors (Docker, DBs, Supabase).

    Each connector has its own ``enabled`` / ``auto_connect`` entry. Secrets
    (DB passwords, Supabase keys) are never stored here — they live in the
    encrypted keystore. This section only carries non-sensitive toggles.
    """

    docker: ResourceEntry = Field(default_factory=lambda: ResourceEntry(enabled=True))
    postgres: ResourceEntry = Field(default_factory=lambda: ResourceEntry(auto_connect=False))
    mysql: ResourceEntry = Field(default_factory=lambda: ResourceEntry(auto_connect=False))
    supabase: ResourceEntry = Field(default_factory=lambda: ResourceEntry(auto_connect=False))


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
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)

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
            errors.append(
                ConfigValidationError(
                    field="providers.default_provider",
                    value=provider_name,
                    reason=(
                        f"Provider '{provider_name}' is not defined in the [providers] section. "
                        f"Add a [{provider_name}] entry or change default_provider."
                    ),
                    severity="CRITICAL",
                )
            )
        else:
            # Remote providers require an API key env var to be populated.
            if provider_entry.api_key_env:
                resolved = os.getenv(provider_entry.api_key_env)
                if not resolved:
                    errors.append(
                        ConfigValidationError(
                            field=f"providers.{provider_name}.api_key_env",
                            value=provider_entry.api_key_env,
                            reason=(
                                f"Environment variable '{provider_entry.api_key_env}' is not set. "
                                f"Provider '{provider_name}' requires an API key to function."
                            ),
                            severity="CRITICAL",
                        )
                    )

        # Optional workspace root — validate if explicitly configured.
        if self.workspace.root is not None:
            wp = Path(self.workspace.root)
            if not wp.exists():
                errors.append(
                    ConfigValidationError(
                        field="workspace.root",
                        value=str(wp),
                        reason=f"Workspace path '{wp}' does not exist.",
                        severity="CRITICAL",
                    )
                )
            elif not os.access(wp, os.R_OK):
                errors.append(
                    ConfigValidationError(
                        field="workspace.root",
                        value=str(wp),
                        reason=f"Workspace path '{wp}' exists but is not readable.",
                        severity="CRITICAL",
                    )
                )

        # Optional memory storage directory — validate writability if configured.
        if self.memory.storage_dir is not None:
            sd = Path(self.memory.storage_dir)
            if not sd.exists():
                errors.append(
                    ConfigValidationError(
                        field="memory.storage_dir",
                        value=str(sd),
                        reason=f"Storage directory '{sd}' does not exist.",
                        severity="WARNING",
                    )
                )
            elif not os.access(sd, os.W_OK):
                errors.append(
                    ConfigValidationError(
                        field="memory.storage_dir",
                        value=str(sd),
                        reason=f"Storage directory '{sd}' exists but is not writable.",
                        severity="CRITICAL",
                    )
                )

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
        resources=ResourcesConfig(),
        display=DisplayConfig(),
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
