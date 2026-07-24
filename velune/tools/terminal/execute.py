from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.core.task_registry import JobRegistry
    from velune.execution.sandbox import SubprocessSandbox

from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.safety import ApprovalMode, classify_command


class ExecuteCommand(BaseTool):
    """Tool for executing terminal commands.

    Approval for ASK-mode calls happens upstream, in the caller's approver
    (see ``ToolLoopRunner._execute_call`` — no tool call reaches ``execute()``
    without a prior approver "yes"). This class only re-checks the two hard
    refusals that must hold regardless of who is calling:
      BLOCK (session-level ApprovalMode) — always raises PermissionError.
      BLOCK (per-command classify_command() verdict) — always raises
      PermissionError, e.g. for destructive commands no approval mode allows.
    """

    def __init__(
        self,
        sandbox: SubprocessSandbox | None = None,
        workspace_path: str | None = None,
        approval_mode: ApprovalMode = ApprovalMode.ASK,
        job_registry: JobRegistry | None = None,
    ):
        self._sandbox = sandbox
        self._workspace_path = workspace_path
        self.approval_mode = approval_mode
        self._job_registry = job_registry

    def get_name(self) -> str:
        return "execute_command"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.TERMINAL_EXECUTE}

    def get_description(self) -> str:
        return "Execute a terminal command"

    async def execute(
        self,
        command: str,
        directory: str | None = None,
        timeout: int = 30,
        background: bool = False,
    ) -> dict:
        """Execute a command after applying the hard ApprovalMode refusals."""
        import asyncio
        from pathlib import Path

        from velune.core.errors.execution import SandboxError
        from velune.execution.command_spec import CommandSpec
        from velune.execution.sandbox import SubprocessSandbox

        verdict = classify_command(command)

        if self.approval_mode == ApprovalMode.BLOCK:
            raise PermissionError(
                f"Command execution is blocked (approval mode: block): {command!r}"
            )

        if verdict.mode == ApprovalMode.BLOCK:
            raise PermissionError(f"Command refused — {verdict.reason}: {command!r}")

        workspace = Path(directory or self._workspace_path or Path.cwd())
        sandbox = self._sandbox or SubprocessSandbox(workspace)

        try:
            spec = CommandSpec.from_string(command, cwd=workspace, timeout=float(timeout))
        except SandboxError as e:
            sandbox.emit_rejection(command, str(e))
            raise e

        import threading

        from velune.execution import cancellation

        if background:
            return await self._execute_background(command, sandbox, spec)

        cancel_event = threading.Event()
        cancellation.register(cancel_event)
        try:
            result = await asyncio.to_thread(sandbox.execute, spec, cancel_event)
        finally:
            cancellation.unregister(cancel_event)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
        }

    async def _execute_background(self, command: str, sandbox, spec) -> dict:
        """Register a JobRecord, launch the sandbox call detached, and return
        immediately — for servers, watchers, or anything long-running. Poll
        with /jobs. Mirrors the shape `_submit_cognition_job`/
        `_submit_background_job` already use for indexing/council jobs
        (`velune/cli/handlers/{cognition,council}.py`), reusing the same
        `JobRegistry` instance so `/jobs` sees these entries too.
        """
        import asyncio
        import threading
        import time

        from velune.core.errors.execution import SandboxError
        from velune.core.task_registry import JobRecord, JobStatus, track
        from velune.execution import cancellation

        if self._job_registry is None:
            raise RuntimeError(
                "Background execution requires a job registry, which isn't available "
                "in this runtime — run the command without background=true."
            )

        job_id = self._job_registry.new_id()
        cancel_event = threading.Event()
        self._job_registry.register(
            JobRecord(
                job_id=job_id, name=f"shell:{command[:40]}", kind="shell", cancel_event=cancel_event
            )
        )

        async def _run() -> None:
            cancellation.register(cancel_event)
            self._job_registry.update(job_id, status=JobStatus.RUNNING)
            try:
                result = await asyncio.to_thread(sandbox.execute, spec, cancel_event)
                preview = (result.stdout or result.stderr or "").strip()[:200]
                self._job_registry.update(
                    job_id,
                    status=JobStatus.COMPLETED,
                    result_preview=preview or f"exit code {result.exit_code}",
                    completed_at=time.monotonic(),
                )
            except asyncio.CancelledError:
                self._job_registry.update(
                    job_id, status=JobStatus.CANCELLED, completed_at=time.monotonic()
                )
                raise
            except SandboxError as exc:
                self._job_registry.update(
                    job_id,
                    status=JobStatus.FAILED,
                    error=str(exc)[:200],
                    completed_at=time.monotonic(),
                )
            finally:
                cancellation.unregister(cancel_event)

        task_obj = asyncio.create_task(_run(), name=f"execute-bg-{job_id}")
        self._job_registry.update(job_id, task=task_obj)
        track(task_obj)

        return {
            "job_id": job_id,
            "status": "started",
            "hint": f"Running detached — check progress with /jobs (job id: {job_id}).",
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to execute",
                },
                "directory": {
                    "type": "string",
                    "description": "Working directory",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Command timeout in seconds",
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run detached; returns immediately with a job id instead of "
                        "waiting for the command to finish. Use for servers, watchers, "
                        "or anything long-running — poll with /jobs."
                    ),
                },
            },
            "required": ["command"],
        }
