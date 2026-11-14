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
        return (
            "Execute a terminal command in the workspace (no shell: pipes, &&, and shell "
            "builtins such as mkdir/del/dir are not available). Use the filesystem tools "
            "(create_directory, delete_directory, move_path, write_file, delete_file) for "
            "file and folder operations."
        )

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import Action, ActionType, Risk
        from velune.permissions.commands import CommandClass, classify

        command = str(args.get("command", ""))
        directory = args.get("directory")
        outside = False
        if directory:
            outside = not boundary.inside(boundary.resolve(directory))
        if not outside:
            from velune.permissions.boundary import command_paths_outside

            outside = bool(command_paths_outside(command, boundary))
        klass, why = classify(command)
        if klass is CommandClass.READ_ONLY:
            from velune.permissions.boundary import command_paths_secret

            # read_file asks before showing a credentials file; a "read-only" command that
            # names one (cat .env) must not be a way around that.
            secret = bool(command_paths_secret(command, boundary))
            return [Action(ActionType.READ, command, why, outside_workspace=outside, secret=secret)]
        return [
            Action(
                ActionType.RUN_COMMAND,
                command,
                why,
                risk={
                    CommandClass.HIGH_RISK: Risk.HIGH,
                    CommandClass.BLOCKED: Risk.HIGH,
                }.get(klass, Risk.MEDIUM),
                outside_workspace=outside,
                blocked=klass is CommandClass.BLOCKED,
                detail=str(directory) if directory else "",
            )
        ]

    async def execute(
        self,
        command: str,
        directory: str | None = None,
        timeout: int | None = None,
        background: bool = False,
    ) -> dict:
        """Execute a command after applying the hard ApprovalMode refusals.

        *timeout* defaults to 30 s in the foreground. A background job runs
        until it is stopped, the session ends, or *timeout* (default
        :data:`~velune.execution.processes.BACKGROUND_TIMEOUT_S`) — a dev
        server must not be killed after 30 seconds.
        """
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

        # The working directory is resolved against the session boundary — a
        # model-supplied ``directory`` used to *become* the workspace, which let
        # any command escape it. It may only be the workspace, a folder inside
        # it, or a location the user granted for this task.
        from velune.permissions.gate import resolve_tool_path

        base = Path(self._workspace_path or Path.cwd())
        workspace = resolve_tool_path(directory, base, label="directory") if directory else base
        sandbox = self._sandbox or SubprocessSandbox(workspace)

        if timeout is None:
            from velune.execution.processes import BACKGROUND_TIMEOUT_S

            timeout = BACKGROUND_TIMEOUT_S if background else 30
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
        from velune.core.task_registry import JobRecord, JobStatus, ProcessLog, track
        from velune.execution.processes import JobProcessObserver

        if self._job_registry is None:
            raise RuntimeError(
                "Background execution requires a job registry, which isn't available "
                "in this runtime — run the command without background=true."
            )

        job_id = self._job_registry.new_id()
        cancel_event = threading.Event()
        self._job_registry.register(
            JobRecord(
                job_id=job_id,
                name=f"shell:{command[:40]}",
                kind="shell",
                cancel_event=cancel_event,
                command=command,
                log=ProcessLog(),
                owner="agent",
            )
        )
        observer = JobProcessObserver(self._job_registry, job_id)

        async def _run() -> None:
            # Deliberately NOT registered with velune.execution.cancellation:
            # that registry is the foreground Ctrl+C path, and pressing Ctrl+C
            # on an unrelated command must not kill a background server. A
            # background job is stopped via JobRegistry.cancel / terminate_job
            # (and at session exit by cleanup_background_jobs).
            self._job_registry.update(job_id, status=JobStatus.RUNNING)
            job = self._job_registry.get(job_id)
            try:
                result = await asyncio.to_thread(sandbox.execute, spec, cancel_event, observer)
                preview = (result.stdout or result.stderr or "").strip()[:200]
                self._job_registry.update(
                    job_id,
                    status=JobStatus.COMPLETED,
                    result_preview=preview or f"exit code {result.exit_code}",
                    exit_code=result.exit_code,
                    completed_at=time.monotonic(),
                )
            except asyncio.CancelledError:
                self._job_registry.update(
                    job_id, status=JobStatus.CANCELLED, completed_at=time.monotonic()
                )
                raise
            except SandboxError as exc:
                # A job stopped on request stays CANCELLED; the sandbox reports
                # the kill as an error, which must not relabel it FAILED.
                self._job_registry.update(
                    job_id,
                    status=JobStatus.CANCELLED if cancel_event.is_set() else JobStatus.FAILED,
                    error=None if cancel_event.is_set() else str(exc)[:200],
                    completed_at=time.monotonic(),
                )
            finally:
                if job is not None and job.log is not None:
                    job.log.flush()

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
                    "description": (
                        "Timeout in seconds (default 30; a background job runs until "
                        "stopped or the session ends)"
                    ),
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
