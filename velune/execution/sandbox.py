"""Isolated command runtime using subprocess execution and resource constraints."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import CommandSpec
from velune.execution.path_guard import is_within_workspace

logger = logging.getLogger("velune.execution.sandbox")


class SandboxResult:
    """The result of executing a command in the sandbox."""

    def __init__(
        self,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: float,
        peak_memory_mb: float,
        peak_cpu_pct: float,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_ms = duration_ms
        self.peak_memory_mb = peak_memory_mb
        self.peak_cpu_pct = peak_cpu_pct

    def to_dict(self) -> dict[str, Any]:
        """Convert result to a standard dictionary representation."""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "peak_memory_mb": self.peak_memory_mb,
            "peak_cpu_pct": self.peak_cpu_pct,
        }


class SubprocessSandbox:
    """Windows-native and POSIX compliant subprocess-isolated execution sandbox."""

    @classmethod
    def for_workspace(cls, workspace_path: Path) -> SubprocessSandbox:
        return cls(workspace_path)

    def __init__(
        self,
        workspace_path: Path,
        max_memory_mb: float = 1024.0,
        max_cpu_percent: float = 90.0,
        allowed_executables: list[str] | None = None,
        bus: Any | None = None,
    ) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.max_memory_mb = max_memory_mb
        self.max_cpu_percent = max_cpu_percent
        self.bus = bus

        if allowed_executables is not None:
            self.allowed_executables = frozenset(allowed_executables)
        else:
            try:
                from velune.kernel.config import ConfigLoader
                config = ConfigLoader(self.workspace_path / "velune.toml").load()
                self.allowed_executables = frozenset(config.execution.allowed_executables)
            except Exception:
                from velune.execution.command_spec import ALLOWED_EXECUTABLES
                self.allowed_executables = ALLOWED_EXECUTABLES

        self.blocked_keywords = [
            "rmdir /s",
            "rd /s",
            "del /f /s /q",
            "format ",
            "mkfs",
            "dd ",
            "rm -rf",
            "shutdown",
            "reboot",
            "poweroff",
            ":(){ :|:& };:",
        ]

    def emit_rejection(self, command: str, reason: str) -> None:
        """Log command rejection at WARN level and emit an event to the CognitiveBus if configured."""
        logger.warning("Command rejected: %s (Reason: %s)", command, reason)
        if self.bus:
            try:
                from velune.kernel.schemas import Event as KernelEvent
                event = KernelEvent(
                    event_type="command_rejected",
                    source="execution",
                    data={"command": command, "reason": reason},
                )
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    loop.create_task(self.bus.emit(event))
                else:
                    asyncio.run(self.bus.emit(event))
            except Exception as e:
                logger.error("Failed to emit command_rejected event to CognitiveBus: %s", e)

    def _is_safe_command(self, cmd: str) -> bool:
        """Check if command contains any blocked patterns, shell chainings, or curl/wget.

        NOTE: This is defense-in-depth only, not the primary security boundary.
        """
        cmd_lower = cmd.lower()

        # Block command chaining / shell piping to enforce atomic execution steps
        for char in ["&&", ";", "|", "||"]:
            if char in cmd:
                return False

        # Block network access or base64 decoding utilities
        blocked_utils = ["curl", "wget", "iwr", "invoke-webrequest", "base64", "frombase64"]
        for util in blocked_utils:
            if util in cmd_lower:
                return False

        for block in self.blocked_keywords:
            if block in cmd_lower:
                return False
        return True

    def _is_safe_path(self, target_path: Path) -> bool:
        """Verify the execution path resides strictly within the workspace."""
        return is_within_workspace(target_path, self.workspace_path)

    def execute(
        self,
        spec: CommandSpec,
    ) -> SandboxResult:
        """Synchronously execute command in process-isolated sandbox."""
        try:
            spec.validate(self.allowed_executables)  # raises SandboxError if not allowed

            if not self._is_safe_path(spec.cwd):
                raise SandboxError(f"Working directory outside workspace: {spec.cwd}")

            # Defense-in-depth check
            cmd_str = " ".join(spec.to_argv())
            if not self._is_safe_command(cmd_str):
                raise SandboxError(f"Command contains blocked pattern or is unsafe (defense-in-depth): {cmd_str}")
        except SandboxError as e:
            cmd_rep = " ".join([spec.executable, *spec.args]) if spec else "unknown"
            self.emit_rejection(cmd_rep, str(e))
            raise e

        run_env = os.environ.copy()
        run_env.update(spec.env_additions)
        # Remove dangerous env vars
        for dangerous in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH"):
            run_env.pop(dangerous, None)

        start_time = time.perf_counter()

        try:
            argv = spec.to_argv()
            logger.debug("Executing command with argv: %s", argv)
            process = subprocess.Popen(
                argv,
                shell=False,           # NEVER shell=True
                cwd=str(spec.cwd),
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            raise SandboxError(f"Failed to spawn subprocess: {e}")

        peak_memory = 0.0
        peak_cpu = 0.0
        ps_proc = None
        try:
            ps_proc = psutil.Process(process.pid)
        except Exception:
            pass

        timeout_occurred = False
        try:
            while process.poll() is None:
                elapsed = time.perf_counter() - start_time
                if elapsed > spec.timeout:
                    timeout_occurred = True
                    break

                if ps_proc:
                    try:
                        mem_info = ps_proc.memory_info()
                        mem_mb = mem_info.rss / (1024 * 1024)
                        if mem_mb > peak_memory:
                            peak_memory = mem_mb

                        cpu_pct = ps_proc.cpu_percent(interval=None)
                        if cpu_pct > peak_cpu:
                            peak_cpu = cpu_pct

                        if mem_mb > self.max_memory_mb:
                            process.terminate()
                            raise SandboxError(
                                f"Memory threshold exceeded: {mem_mb:.2f}MB > {self.max_memory_mb}MB"
                            )

                    except psutil.NoSuchProcess:
                        pass

                time.sleep(0.05)
        except Exception as e:
            if process.poll() is None:
                process.kill()
            raise e

        if timeout_occurred:
            process.kill()
            stdout, stderr = process.communicate()
            duration_ms = (time.perf_counter() - start_time) * 1000
            raise SandboxError(
                f"Command timed out after {spec.timeout} seconds.\nStdout: {stdout}\nStderr: {stderr}"
            )

        stdout, stderr = process.communicate()
        duration_ms = (time.perf_counter() - start_time) * 1000

        return SandboxResult(
            exit_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_ms=duration_ms,
            peak_memory_mb=peak_memory,
            peak_cpu_pct=peak_cpu,
        )

