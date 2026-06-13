"""Isolated command runtime using subprocess execution and resource constraints."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import CommandSpec
from velune.execution.path_guard import is_within_workspace

logger = logging.getLogger("velune.execution.sandbox")

#: Per-stream cap on captured subprocess output. Output beyond this is drained
#: (so the child never blocks on a full pipe) but discarded, bounding the
#: parent's memory regardless of how much the child writes.
DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MiB


class _BoundedStreamReader(threading.Thread):
    """Drain a subprocess text pipe into a memory-bounded buffer on its own thread.

    Both pipes must be read *concurrently* with the process running. The old
    design polled for exit and only called ``communicate()`` afterwards; a child
    that wrote more than the OS pipe capacity (~64 KiB) would block on ``write``,
    never exit, and the parent would kill it as a false timeout with its output
    lost. Draining on a dedicated thread removes that deadlock, and the byte cap
    keeps a runaway producer from exhausting memory.
    """

    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    break
                if self._size >= self._limit:
                    # Keep draining to EOF so the writer never blocks, but
                    # discard the overflow.
                    self.truncated = True
                    continue
                allowed = self._limit - self._size
                if len(chunk) > allowed:
                    self._chunks.append(chunk[:allowed])
                    self._size += allowed
                    self.truncated = True
                else:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
        except (ValueError, OSError):
            # Stream closed underneath us (e.g. after a process-tree kill).
            pass
        finally:
            try:
                self._stream.close()
            except Exception:
                pass

    def text(self) -> str:
        result = "".join(self._chunks)
        if self.truncated:
            result += f"\n[velune: output truncated at {self._limit} bytes]"
        return result


def _kill_process_tree(pid: int) -> None:
    """Terminate a process and all of its descendants.

    ``Popen.kill()`` only signals the direct child; anything it spawned
    (test runners, build tools, watchers) would survive a timeout or
    memory-limit kill and keep running unsupervised. psutil lets us walk
    and kill the whole tree portably on Windows and POSIX.
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for proc in (*children, parent):
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as e:
            logger.warning("Failed to kill process %s in sandbox tree: %s", proc.pid, e)
    psutil.wait_procs([*children, parent], timeout=3)


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
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.max_memory_mb = max_memory_mb
        self.max_cpu_percent = max_cpu_percent
        self.max_output_bytes = max_output_bytes
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

                if loop is not None:
                    from velune.core.task_registry import track

                    track(loop.create_task(self.bus.emit(event), name="command_rejected_emit"))
                # No running loop: emission is skipped; rejection is already
                # recorded via logger.warning() above.
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
                raise SandboxError(
                    f"Command contains blocked pattern or is unsafe (defense-in-depth): {cmd_str}"
                )
        except SandboxError as e:
            cmd_rep = " ".join([spec.executable, *spec.args]) if spec else "unknown"
            self.emit_rejection(cmd_rep, str(e))
            raise e

        run_env = os.environ.copy()
        run_env.update(spec.env_additions)
        # Remove dangerous env vars
        for dangerous in (
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
            "PYTHONINSPECT",
            "BASH_ENV",
            "ENV",
            "PROMPT_COMMAND",
        ):
            run_env.pop(dangerous, None)
        run_env["PYTHONNOUSERSITE"] = "1"

        start_time = time.perf_counter()

        try:
            argv = spec.to_argv()
            logger.debug("Executing command with argv: %s", argv)
            process = subprocess.Popen(
                argv,
                shell=False,  # NEVER set shell to True
                cwd=str(spec.cwd),
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            raise SandboxError(f"Failed to spawn subprocess: {e}")

        # Drain both pipes concurrently while the process runs. Reading only
        # after exit (the old ``communicate()`` placement) deadlocks once the
        # child fills the OS pipe buffer — see _BoundedStreamReader.
        stdout_reader = _BoundedStreamReader(process.stdout, self.max_output_bytes)
        stderr_reader = _BoundedStreamReader(process.stderr, self.max_output_bytes)
        stdout_reader.start()
        stderr_reader.start()

        def _join_readers() -> tuple[str, str]:
            # After the process exits or its tree is killed, the pipes reach EOF
            # and the reader threads finish promptly. The join timeout is a guard
            # against a wedged thread, not the expected path.
            stdout_reader.join(timeout=3.0)
            stderr_reader.join(timeout=3.0)
            return stdout_reader.text(), stderr_reader.text()

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
                            _kill_process_tree(process.pid)
                            _join_readers()
                            raise SandboxError(
                                f"Memory threshold exceeded: {mem_mb:.2f}MB > {self.max_memory_mb}MB"
                            )

                    except psutil.NoSuchProcess:
                        pass

                time.sleep(0.05)  # OK: runs in thread via asyncio.to_thread(), not event loop
        except Exception as e:
            if process.poll() is None:
                _kill_process_tree(process.pid)
            _join_readers()
            raise e

        if timeout_occurred:
            _kill_process_tree(process.pid)
            stdout, stderr = _join_readers()
            duration_ms = (time.perf_counter() - start_time) * 1000
            raise SandboxError(
                f"Command timed out after {spec.timeout} seconds.\nStdout: {stdout}\nStderr: {stderr}"
            )

        stdout, stderr = _join_readers()
        duration_ms = (time.perf_counter() - start_time) * 1000

        return SandboxResult(
            exit_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_ms=duration_ms,
            peak_memory_mb=peak_memory,
            peak_cpu_pct=peak_cpu,
        )
