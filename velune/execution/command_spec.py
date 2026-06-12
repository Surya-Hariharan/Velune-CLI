"""Command specification representation and safe shell-string parsing logic."""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.core.errors.execution import SandboxError

ALLOWED_EXECUTABLES = frozenset(
    {
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
    }
)


TRUSTED_PATH_PREFIXES: frozenset[str] = frozenset(
    {
        "/usr/bin/",
        "/usr/local/bin/",
        "/bin/",
        "/usr/sbin/",
        # macOS
        "/opt/homebrew/bin/",
        "/usr/local/opt/",
    }
)

#: Virtual-environment directory names trusted when rooted in the workspace.
_WORKSPACE_VENV_NAMES = (".venv", "venv")


def _is_trusted_path(
    resolved_path: Path,
    workspace: Path | None = None,
    platform: str | None = None,
) -> bool:
    """Verify executable lives in a trusted system directory or a known venv.

    A venv binary is trusted only when it belongs to the interpreter's own
    environment (``sys.prefix``/``sys.base_prefix``) or to a ``.venv``/``venv``
    directory rooted directly in *workspace* (defaults to the process CWD).
    A bare substring match on ".venv" is NOT sufficient — that would let any
    attacker-writable path like ``/tmp/.venv/evil`` defeat the check.
    """
    if (platform or sys.platform) == "win32":
        return True  # Windows PATH hijacking is different threat model

    path_str = str(resolved_path)
    for prefix in TRUSTED_PATH_PREFIXES:
        if path_str.startswith(prefix):
            return True

    # The running interpreter's environment (covers the active venv)
    for env_root in (sys.prefix, sys.base_prefix):
        try:
            if resolved_path.is_relative_to(Path(env_root).resolve()):
                return True
        except (OSError, ValueError):
            continue

    # Venvs rooted directly in the workspace
    root = (workspace or Path.cwd()).resolve()
    for venv_name in _WORKSPACE_VENV_NAMES:
        candidate = root / venv_name
        try:
            if candidate.is_dir() and resolved_path.is_relative_to(candidate.resolve()):
                return True
        except (OSError, ValueError):
            continue

    return False


@dataclass(frozen=True)
class CommandSpec:
    """Strongly-typed process execution specification ensuring shell=False safety."""

    executable: str  # basename only, e.g. "pytest"
    args: tuple[str, ...]  # individual arguments
    cwd: Path
    timeout: float = 60.0
    env_additions: dict[str, str] = field(default_factory=dict)

    def validate(self, allowed_executables: frozenset[str] | None = None) -> None:
        """Validate the executable basename against allowlists and system path boundaries."""
        if allowed_executables is None:
            try:
                from velune.kernel.config import ConfigLoader

                config = ConfigLoader().load()
                allowed = frozenset(config.execution.allowed_executables)
            except Exception:
                allowed = ALLOWED_EXECUTABLES
        else:
            allowed = allowed_executables

        if self.executable not in allowed:
            raise SandboxError(
                f"Executable '{self.executable}' is not in the allowed list. "
                f"Permitted: {sorted(allowed)}"
            )

        exe_path = shutil.which(self.executable)
        if exe_path is None:
            raise SandboxError(f"Executable '{self.executable}' not found in PATH")

        resolved = Path(exe_path).resolve()
        # Must be an absolute path to a real binary, not a shell builtin
        if not resolved.is_absolute():
            raise SandboxError(f"Executable resolves to non-absolute path: {resolved}")

        # Verify trusted path
        if not _is_trusted_path(resolved, workspace=self.cwd):
            raise SandboxError(
                f"Executable '{self.executable}' resolved to untrusted path: {resolved}. "
                f"Possible PATH hijacking. Expected path in: {TRUSTED_PATH_PREFIXES}"
            )

        # Pin the path that passed validation so to_argv() executes exactly this
        # binary, closing the TOCTOU window between validate() and execution.
        # (frozen dataclass — bypass immutability for this internal cache only)
        object.__setattr__(self, "_validated_exe", str(resolved))

    def to_argv(self) -> list[str]:
        """Convert specification into ready-to-run argv list with resolved executable path.

        Prefers the path pinned by a prior ``validate()`` call so the binary
        that was security-checked is the one actually executed.
        """
        validated = getattr(self, "_validated_exe", None)
        if validated is not None:
            return [validated, *self.args]
        exe_path = shutil.which(self.executable)
        resolved_exe = exe_path if exe_path is not None else self.executable
        return [resolved_exe, *self.args]

    @classmethod
    def from_string(cls, cmd: str, cwd: Path, timeout: float = 60.0) -> CommandSpec:
        """Parse a command string safely using shlex. Raises SandboxError if unsafe."""
        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            raise SandboxError(f"Malformed command string: {e}") from e

        if not parts:
            raise SandboxError("Empty command")

        # Reject inline shell operators even in argument position
        for part in parts:
            for bad in (";", "&&", "||", "|", "`", "$( ", "$(", "${"):
                if bad in part:
                    raise SandboxError(f"Shell operator '{bad}' found in command argument")

        return cls(
            executable=Path(parts[0]).name,  # basename only
            args=tuple(parts[1:]),
            cwd=cwd,
            timeout=timeout,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert command specification to a JSON serializable dict for checkpoint state."""
        return {
            "executable": self.executable,
            "args": list(self.args),
            "cwd": str(self.cwd),
            "timeout": self.timeout,
            "env_additions": self.env_additions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandSpec:
        """Load command specification from checkpoint state representation."""
        return cls(
            executable=data["executable"],
            args=tuple(data.get("args") or []),
            cwd=Path(data["cwd"]),
            timeout=float(data.get("timeout", 60.0)),
            env_additions=dict(data.get("env_additions") or {}),
        )
