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
        "Run `velune setup` for a 2-minute guided provider + model setup",
        "Or run `velune models scan` to discover local Ollama / LM Studio models",
        "Run `velune doctor` to diagnose provider connectivity",
    ]


class RateLimitError(VeluneError):
    title = "Rate limit reached"
    cause = "The cloud provider has temporarily limited requests from this API key."
    fix = [
        "Wait a moment and retry the command",
        "Use a local model (Ollama) as a fallback to avoid rate limits",
        "Check your provider's dashboard for quota and usage details",
    ]


# ---------------------------------------------------------------------------
# Workspace / configuration errors
# ---------------------------------------------------------------------------


class WorkspaceNotInitializedError(VeluneError):
    title = "Workspace not initialized"
    cause = "This directory does not contain a Velune workspace (velune.toml not found)."
    fix = [
        "Run `velune init` to initialize this directory as a Velune workspace",
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


# ---------------------------------------------------------------------------
# Repository / indexing errors
# ---------------------------------------------------------------------------
