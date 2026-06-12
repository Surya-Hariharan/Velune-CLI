"""Structured user-facing error catalog.

Every error type carries a short title, a cause explanation, and actionable
fix steps.  The CLI renders these via ``velune.cli.rendering.error_panel``
instead of printing raw exception text.
"""

from __future__ import annotations


class VeluneError(Exception):
    """Base class for all user-facing Velune errors with rich display metadata.

    Subclasses declare ``title``, ``cause``, ``fix``, and optionally
    ``docs_url`` as class attributes.  The ``__init__`` accepts an optional
    *detail* string (e.g. the underlying exception message) and an optional
    *cause_override* to substitute the class-level ``cause`` at runtime when
    the root cause is known precisely.
    """

    title: str = "An unexpected error occurred"
    cause: str = "An internal error was encountered."
    fix: list[str] = ["Run `velune doctor` to diagnose your environment."]
    docs_url: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        cause_override: str | None = None,
    ) -> None:
        self._detail = detail
        self._cause_override = cause_override
        super().__init__(detail or self.title)

    def get_cause(self) -> str:
        return self._cause_override or self.cause

    def get_detail(self) -> str | None:
        return self._detail


# ---------------------------------------------------------------------------
# Provider / model errors
# ---------------------------------------------------------------------------

class OllamaNotRunningError(VeluneError):
    title = "Ollama is not running"
    cause = (
        "Velune tried to connect to Ollama at localhost:11434 but the "
        "connection was refused."
    )
    fix = [
        "Run `ollama serve` in a separate terminal window",
        "Then retry your command, or run `velune doctor` to verify all providers",
    ]


class ModelNotFoundError(VeluneError):
    title = "Model not found"
    cause = "The requested model ID is not registered in the Velune model catalog."
    fix = [
        "Run `velune models scan` to discover available models",
        "Check the model ID for typos",
        "Run `velune models list` to see all registered models",
    ]


class NoModelsAvailableError(VeluneError):
    title = "No models available"
    cause = "No models are configured for any provider in this workspace."
    fix = [
        "Run `velune models scan` to discover local Ollama models",
        "Configure an API key with `velune config set providers.anthropic.api_key <key>`",
        "Run `velune doctor` to diagnose provider connectivity",
    ]


class APIKeyMissingError(VeluneError):
    title = "API key missing"
    cause = (
        "A cloud provider is configured but no API key was found in the "
        "environment or keystore."
    )
    fix = [
        "Set the API key with `velune config set providers.<name>.api_key <key>`",
        "Or export it as an environment variable (e.g. ANTHROPIC_API_KEY=...)",
        "Run `velune doctor` to see which providers are missing keys",
    ]


class ProviderUnavailableError(VeluneError):
    title = "Provider unavailable"
    cause = (
        "The model provider failed its health check and cannot accept "
        "inference requests."
    )
    fix = [
        "Run `velune doctor check` to identify which providers are unreachable",
        "Verify the provider service is running (e.g. `ollama serve` for Ollama)",
        "Check API key and network connectivity for cloud providers",
    ]


class RateLimitError(VeluneError):
    title = "Rate limit reached"
    cause = "The cloud provider has temporarily limited requests from this API key."
    fix = [
        "Wait a moment and retry the command",
        "Use a local model (Ollama) as a fallback to avoid rate limits",
        "Check your provider's dashboard for quota and usage details",
    ]


class ContextWindowExceededError(VeluneError):
    title = "Context window exceeded"
    cause = (
        "The combined prompt and context is larger than the model's maximum "
        "context length."
    )
    fix = [
        "Use a model with a larger context window (`velune models list`)",
        "Reduce the amount of repository context by narrowing the file selection",
        "Split the task into smaller, focused sub-tasks",
    ]


class InsufficientVRAMError(VeluneError):
    title = "Insufficient VRAM"
    cause = (
        "The selected local model requires more GPU memory than is currently "
        "available."
    )
    fix = [
        "Close other GPU-intensive applications to free VRAM",
        "Use a smaller quantized version of the model (e.g. q4_K_M instead of q8_0)",
        "Run `velune doctor` to view your hardware profile and compatible models",
    ]


# ---------------------------------------------------------------------------
# Workspace / configuration errors
# ---------------------------------------------------------------------------

class WorkspaceNotInitializedError(VeluneError):
    title = "Workspace not initialized"
    cause = (
        "This directory does not contain a Velune workspace "
        "(velune.toml not found)."
    )
    fix = [
        "Run `velune workspace init` to initialize this directory as a Velune workspace",
        "Or change to a directory that already contains a velune.toml",
        "Use `--workspace <path>` to point to an existing workspace",
    ]


# ---------------------------------------------------------------------------
# Security errors
# ---------------------------------------------------------------------------

class PathTraversalError(VeluneError):
    title = "Path traversal blocked"
    cause = (
        "A file path resolved to a location outside the workspace root. "
        "This is a security violation."
    )
    fix = [
        "Ensure all file paths are relative to the workspace root",
        "Do not use `..` sequences or absolute paths that escape the workspace",
        "Run `velune workspace info` to confirm your current workspace root",
    ]


class SSRFAttemptError(VeluneError):
    title = "Internal network request blocked"
    cause = (
        "A web tool attempted to reach an internal or metadata network address "
        "(e.g. cloud provider IMDS, RFC 1918 private ranges). "
        "This request was blocked to prevent SSRF attacks."
    )
    fix = [
        "Only fetch publicly accessible URLs",
        "Avoid internal network addresses (169.254.x.x, 10.x.x.x, 192.168.x.x, etc.)",
        "If this was unexpected, inspect workspace content for injected URLs",
    ]


# ---------------------------------------------------------------------------
# Repository / indexing errors
# ---------------------------------------------------------------------------

class IndexingFailedError(VeluneError):
    title = "Repository indexing failed"
    cause = "Velune encountered an error while indexing the repository AST and structure."
    fix = [
        "Run `velune workspace reindex` to force a fresh index",
        "Check file permissions in the workspace directory",
        "Run `velune doctor` to verify the workspace is correctly configured",
    ]
