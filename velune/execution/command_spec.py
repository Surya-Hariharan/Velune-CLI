"""Command specification representation and safe shell-string parsing logic."""

from __future__ import annotations

import os
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


def _windows_trusted_roots() -> list[Path]:
    """Resolve the directory roots that hold trusted executables on Windows.

    These are the system and program-install locations a non-privileged
    attacker generally cannot write to. Derived from environment variables
    (which the user controls but malware typically does not rewrite) with
    hard-coded fallbacks so the guard still functions if a variable is unset.
    Per-user program installs (``%LOCALAPPDATA%\\Programs``) are included
    because that is where python.org, nvm-windows, and similar place real
    interpreters.
    """
    roots: list[Path] = []
    for env_var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(env_var)
        if value:
            roots.append(Path(value))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "Programs")
    # Fallbacks if the environment is missing the variables above.
    roots.extend(
        (
            Path(r"C:\Windows"),
            Path(r"C:\Program Files"),
            Path(r"C:\Program Files (x86)"),
        )
    )
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return resolved


def _is_trusted_path(
    resolved_path: Path,
    workspace: Path | None = None,
    platform: str | None = None,
) -> bool:
    """Verify an executable lives in a trusted system directory or a known venv.

    A venv binary is trusted only when it belongs to the interpreter's own
    environment (``sys.prefix``/``sys.base_prefix``) or to a ``.venv``/``venv``
    directory rooted directly in *workspace* (defaults to the process CWD).
    A bare substring match on ".venv" is NOT sufficient — that would let any
    attacker-writable path like ``/tmp/.venv/evil`` defeat the check.

    Windows is NOT permissive: the resolved binary must live under a system or
    program-install root (see :func:`_windows_trusted_roots`), the interpreter's
    own environment, or a workspace venv. This blocks PATH hijacking where a
    malicious ``git.exe`` planted in an attacker-writable directory earlier in
    ``PATH`` would otherwise be executed.
    """
    plat = platform or sys.platform

    # Common to every platform: the running interpreter's environment (this
    # covers the active venv) and venvs rooted directly in the workspace.
    for env_root in (sys.prefix, sys.base_prefix):
        try:
            if resolved_path.is_relative_to(Path(env_root).resolve()):
                return True
        except (OSError, ValueError):
            continue

    root = (workspace or Path.cwd()).resolve()
    for venv_name in _WORKSPACE_VENV_NAMES:
        candidate = root / venv_name
        try:
            if candidate.is_dir() and resolved_path.is_relative_to(candidate.resolve()):
                return True
        except (OSError, ValueError):
            continue

    if plat == "win32":
        for trusted_root in _windows_trusted_roots():
            try:
                if resolved_path.is_relative_to(trusted_root):
                    return True
            except (OSError, ValueError):
                continue
        return False

    path_str = str(resolved_path)
    return any(path_str.startswith(prefix) for prefix in TRUSTED_PATH_PREFIXES)


#: Single-dash flags that turn an allowlisted interpreter into an arbitrary-code
#: engine by taking program text directly on the command line. Blocking these
#: closes the no-approval RCE path (``execute_command`` runs without a diff/
#: approval gate); running a *file* still requires the file to exist, and any
#: agent-authored file must first pass the DiffPreview write-approval flow.
_INTERPRETER_INLINE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print", "--eval-stdin"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print", "--eval-stdin"}),
}


def _find_inline_code_flag(executable: str, args: tuple[str, ...]) -> str | None:
    """Return the first inline-code flag found for *executable*, or ``None``.

    Handles Python short-flag clustering (``-Ic`` is equivalent to ``-I -c``)
    so ``python -Ic '<code>'`` cannot slip past an exact ``-c`` match. No
    legitimate single-dash Python flag other than ``-c`` contains the letter
    ``c``, so a cluster containing ``c`` is an unambiguous inline-code request.
    """
    exe = Path(executable).name.lower()
    if exe in ("python", "python3"):
        for arg in args:
            if arg == "-c":
                return arg
            # Short-flag cluster (single dash, not "--…") containing 'c'.
            if len(arg) >= 2 and arg[0] == "-" and arg[1] != "-" and "c" in arg:
                return arg
        return None
    blocked = _INTERPRETER_INLINE_FLAGS.get(exe)
    if blocked:
        for arg in args:
            if arg in blocked:
                return arg
    return None


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

        # Interpreters on the allowlist (python, node, …) can otherwise execute
        # arbitrary inline program text, escaping the workspace boundary with no
        # approval gate. Reject the inline-code flags; running a file is still
        # permitted (and any agent-authored file goes through write-approval).
        inline_flag = _find_inline_code_flag(self.executable, self.args)
        if inline_flag is not None:
            raise SandboxError(
                f"Inline-code execution flag '{inline_flag}' is not permitted for "
                f"interpreter '{self.executable}'. Run a script file in the workspace "
                f"instead — inline code bypasses the workspace boundary."
            )

        exe_path = shutil.which(self.executable)
        if exe_path is None:
            raise SandboxError(f"Executable '{self.executable}' not found in PATH")

        symlink_path = Path(exe_path)
        resolved = symlink_path.resolve()
        # Must be an absolute path to a real binary, not a shell builtin
        if not resolved.is_absolute():
            raise SandboxError(f"Executable resolves to non-absolute path: {resolved}")

        # Verify trusted path — check both the symlink location (from shutil.which)
        # AND the resolved path.  On macOS, Homebrew symlinks binaries from a trusted
        # bin dir (e.g. /opt/homebrew/bin/git) into the Cellar versioned path
        # (/opt/homebrew/Cellar/git/x.y.z/bin/git).  The Cellar path is not in
        # TRUSTED_PATH_PREFIXES, but the symlink location is — and if the symlink
        # itself lives in a trusted directory, the binary it points to is trusted.
        if not (
            _is_trusted_path(resolved, workspace=self.cwd)
            or _is_trusted_path(symlink_path, workspace=self.cwd)
        ):
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
