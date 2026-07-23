from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.core.task_registry import JobRegistry
    from velune.execution.sandbox import SubprocessSandbox

from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.safety import ApprovalMode, classify_command


class ExecuteCommand(BaseTool):
    """Tool for executing terminal commands.

    Respects the session-level ApprovalMode:
      SAFE   — runs without confirmation (if command is also read-only).
      ASK    — raises PermissionError so the REPL can prompt the user.
      BLOCK  — always raises PermissionError.
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
        """Execute a command after applying the current ApprovalMode gate."""
        import asyncio
        from pathlib import Path

        from velune.core.errors.execution import SandboxError
        from velune.execution.command_spec import CommandSpec
        from velune.execution.sandbox import SubprocessSandbox

        # --- ApprovalMode gate -------------------------------------------
        verdict = classify_command(command)

        if self.approval_mode == ApprovalMode.BLOCK:
            raise PermissionError(
                f"Command execution is blocked (approval mode: block): {command!r}"
            )

        if verdict.mode == ApprovalMode.BLOCK:
            raise PermissionError(f"Command refused — {verdict.reason}: {command!r}")

        # Check instance-level allowed commands cache
        if not hasattr(self, "_allowed_commands"):
            self._allowed_commands = set()

        if self.approval_mode == ApprovalMode.ASK and verdict.mode != ApprovalMode.SAFE:
            if command not in self._allowed_commands:
                try:
                    from prompt_toolkit.application.current import get_app

                    app = get_app()

                    if app is not None and app.is_running:

                        def _ask_user() -> str:
                            from rich.console import Console
                            from rich.markup import escape

                            out = Console()
                            out.print("\n[bold yellow]Velune wants to execute:[/bold yellow]")
                            out.print(f"  [bold cyan]{escape(command)}[/bold cyan]")
                            if directory:
                                out.print(f"  [dim]({escape('in ' + directory)})[/dim]")
                            out.print("\nChoose:")
                            out.print("  [1] Allow once")
                            out.print("  [2] Always allow for this session")
                            out.print("  [3] Skip")
                            out.print("  [4] Cancel")
                            while True:
                                try:
                                    choice = out.input("[bold]Your choice (1-4): [/bold]").strip()
                                    if choice in ("1", "2", "3", "4"):
                                        return choice
                                except (KeyboardInterrupt, EOFError):
                                    return "4"

                        choice = await app.run_in_terminal(_ask_user)

                        if choice == "1":
                            pass  # allow once
                        elif choice == "2":
                            self._allowed_commands.add(command)
                        elif choice == "3":
                            return {
                                "exit_code": 0,
                                "stdout": "Skipped by user",
                                "stderr": "",
                                "duration_ms": 0,
                            }
                        else:
                            raise PermissionError(f"Command cancelled by user: {command!r}")
                    else:
                        raise PermissionError(f"__approval_required__:{command}")
                except ImportError:
                    raise PermissionError(f"__approval_required__:{command}")
        # -----------------------------------------------------------------

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
            JobRecord(job_id=job_id, name=f"shell:{command[:40]}", cancel_event=cancel_event)
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
