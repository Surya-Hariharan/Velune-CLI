"""Path traversal protection for all filesystem and git operations.

Every tool that accepts a path from user/LLM input must call
``PathGuard.validate()`` before using that path in any file or subprocess
operation.  The guard resolves symlinks and asserts that the canonical path
stays inside the declared workspace root.
"""

from __future__ import annotations

from pathlib import Path


class PathTraversalError(ValueError):
    """Raised when a path resolves to a location outside the workspace root.

    Inherits from ValueError so that tool execution layers that catch
    ValueError also handle traversal attempts consistently.
    """


class PathGuard:
    """Validates that resolved paths remain within a workspace root.

    Usage::

        guard = PathGuard(workspace_root)
        safe_path = guard.validate(user_supplied_path)   # raises PathTraversalError
        with open(safe_path) as f: ...
    """

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()

    def validate(self, path: Path | str) -> Path:
        """Resolve *path* and assert it is within ``workspace_root``.

        Returns the canonicalised ``Path`` on success.
        Raises ``PathTraversalError`` if the resolved path escapes the workspace.
        """
        resolved = Path(path).resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise PathTraversalError(
                f"Path '{path}' resolves to '{resolved}', which is outside workspace "
                f"root '{self.workspace_root}'."
            )
        return resolved


def resolve_in_workspace(path: Path | str, workspace: Path, label: str = "path") -> Path:
    """Anchor *path* to *workspace* and return its validated, canonical form.

    Relative paths are resolved against the workspace root — not the process
    CWD, which may differ and would silently redirect the operation elsewhere.
    Raises ``PathTraversalError`` if the result escapes the workspace.

    All filesystem tools must perform I/O on the path returned here, never on
    the raw input path.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    try:
        return PathGuard(workspace).validate(candidate)
    except PathTraversalError as exc:
        raise PathTraversalError(f"{label}: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Legacy functional API (kept for backward compatibility with read.py / write.py)
# ─────────────────────────────────────────────────────────────────────────────


def is_within_workspace(path: Path, workspace: Path) -> bool:
    """Return True if *path* is inside *workspace* (both are resolved first)."""
    try:
        guard = PathGuard(workspace)
        guard.validate(path)
        return True
    except (PathTraversalError, OSError):
        return False


def validate_workspace_path(path: Path, workspace: Path, label: str = "path") -> None:
    """Raise ValueError if *path* is outside *workspace*.

    Delegates to ``PathGuard.validate()`` and re-wraps ``PathTraversalError``
    as ``ValueError`` for callers that pre-date the typed exception.
    """
    try:
        PathGuard(workspace).validate(path)
    except PathTraversalError as exc:
        raise ValueError(f"Security: {label} — {exc}") from exc
