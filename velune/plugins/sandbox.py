"""Subprocess-based plugin sandbox.

Each hook invocation runs in a freshly-spawned Python process with:
  - No inherited environment variables (only PATH/SystemRoot for process creation)
  - cwd set to a temporary directory (not workspace root)
  - Wall-clock timeout
  - stdout/stderr piped and bounded
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.plugins.sandbox")

_RUNNER = Path(__file__).parent / "runner.py"
_DEFAULT_TIMEOUT_S = 30


class PluginSandboxError(RuntimeError):
    """Raised when a sandboxed plugin times out, crashes, or returns an error."""


def _safe_env() -> dict[str, str]:
    """Minimal environment for the sandbox subprocess — no credentials, no secrets."""
    env: dict[str, str] = {}
    # PATH is needed to locate python/system DLLs on both platforms.
    # On Windows, SystemRoot is required for subprocess creation.
    keep = (
        {"PATH", "SystemRoot", "SYSTEMROOT", "windir", "WINDIR"}
        if sys.platform == "win32"
        else {"PATH"}
    )
    for key in keep:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


class PluginSandbox:
    """Run a single plugin hook call in an isolated subprocess.

    Each call to :meth:`run_hook` spawns a fresh Python process, sends the
    request as a JSON line on stdin, reads the JSON response from stdout, and
    returns the result.  The subprocess has no access to parent env vars
    (credentials, API keys, workspace paths) and cannot reach the calling
    process's memory.
    """

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT_S) -> None:
        self.timeout = timeout

    def run_hook(
        self,
        plugin_dir: Path,
        entry_point: str,
        class_name: str,
        hook_name: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Invoke *hook_name* on the plugin at *plugin_dir* in a sandboxed subprocess.

        Returns the hook's return value (must be JSON-serialisable) or None.
        Raises :exc:`PluginSandboxError` on timeout, subprocess failure, or
        hook-level error.
        """
        request = json.dumps(
            {
                "plugin_dir": str(plugin_dir),
                "entry_point": entry_point,
                "class_name": class_name,
                "hook_name": hook_name,
                "payload": payload or {},
            }
        )

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": _safe_env(),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        with tempfile.TemporaryDirectory(prefix="velune_plugin_") as tmpdir:
            popen_kwargs["cwd"] = tmpdir
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(_RUNNER)],
                    **popen_kwargs,
                )
                stdout, stderr = proc.communicate(input=request.encode(), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise PluginSandboxError(
                    f"Plugin hook '{hook_name}' timed out after {self.timeout}s"
                )
            except OSError as exc:
                raise PluginSandboxError(f"Failed to spawn plugin sandbox process: {exc}") from exc

        if stderr:
            logger.debug("Plugin sandbox stderr: %s", stderr.decode(errors="replace"))

        if not stdout.strip():
            raise PluginSandboxError(
                f"Plugin hook '{hook_name}' produced no output (exit code {proc.returncode})"
            )

        try:
            response = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise PluginSandboxError(f"Plugin sandbox returned malformed JSON: {exc}") from exc

        if response.get("error"):
            raise PluginSandboxError(
                f"Plugin hook '{hook_name}' raised an error: {response['error']}"
            )

        return response.get("result")
