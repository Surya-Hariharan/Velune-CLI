"""The ``process`` connector: operate background commands the agent started.

``execute_command(background=true)`` starts a long-running command (a dev
server, a watcher) and returns a job id. This connector gives the model the
rest of the lifecycle — what is running, its recent output, whether it is ready
and how to stop it — through the same resource pipeline as every other
integration, so each call is described, authorized by the permission gate,
audited and redacted like any other.

Boundaries, all enforced here rather than by instruction to the model:

* Only **shell jobs the agent started** are visible. A job started by the user
  (``/run --bg``), indexing, or anything else answers "no such job" — the
  existence of other jobs is not revealed and they can never be stopped here.
* Jobs are addressed by **job id only**. The operating-system process id is
  never shown and never accepted, so this cannot become "kill any process".
  Stopping delegates to :func:`velune.execution.processes.terminate_job`, which
  also refuses to signal a recycled process id.
* Readiness checks take **literal log text**, not a regular expression, so a
  model-supplied pattern cannot cause catastrophic backtracking, and port checks
  are limited to loopback by :func:`~velune.execution.processes.wait_until_ready`.
* Output is bounded (lines and characters) and passes through the manager's
  redaction before it reaches the model.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from velune.core.redaction import redact_secrets
from velune.resources.base import (
    DiscoveryHint,
    ErrorCode,
    Lifecycle,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)

if TYPE_CHECKING:
    from velune.core.task_registry import JobRecord, JobRegistry

_JOB_ID = re.compile(r"^job-\d{1,8}$")
_MAX_LOG_LINES = 500
_MAX_LINE_CHARS = 500
_MAX_WAIT_S = 60.0
_DEFAULT_TAIL = 100

_JOB = {"type": "string", "description": "Job id returned when the command was started (job-0001)."}


class ProcessConnector(ResourceConnector):
    """Status, logs, readiness and stop for background commands the agent started."""

    resource_id = "process"
    display_name = "Background processes"
    lifecycle = Lifecycle.SUPERVISED_PROCESS
    description = (
        "Background commands you started (dev servers, watchers): status, recent output, "
        "readiness checks and stop."
    )
    keywords = (
        "background",
        "job",
        "jobs",
        "server",
        "watcher",
        "logs",
        "readiness",
        "listening",
    )

    def __init__(self, registry: JobRegistry, name: str | None = None) -> None:
        super().__init__(name)
        self._registry = registry

    # ── Lifecycle: nothing to open ───────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        return ResourceResult.success("connect")

    async def disconnect(self) -> ResourceResult:
        return ResourceResult.success("disconnect")

    def status(self) -> ResourceStatus:
        count = len(self._visible_jobs())
        return ResourceStatus(
            self.resource_id,
            self.display_name,
            ResourceState.CONNECTED,
            detail=f"{count} background job(s)",
        )

    async def discover(self) -> list[DiscoveryHint]:
        return []

    async def probe_availability(self) -> tuple[bool, str]:
        return True, ""

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        return [
            ResourceCapability(
                "list",
                read,
                "List the background commands you started",
                scope="process.job.list",
                category="jobs",
            ),
            ResourceCapability(
                "status",
                read,
                "Status of one background command (running, exit code, ready)",
                scope="process.job.status",
                category="jobs",
                params={"properties": {"job_id": _JOB}, "required": ["job_id"]},
            ),
            ResourceCapability(
                "logs",
                read,
                "Recent output lines of a background command",
                scope="process.job.logs",
                category="jobs",
                params={
                    "properties": {
                        "job_id": _JOB,
                        "tail": {
                            "type": "integer",
                            "description": f"Lines from the end (default {_DEFAULT_TAIL}).",
                            "minimum": 1,
                            "maximum": _MAX_LOG_LINES,
                        },
                        "stream": {
                            "type": "string",
                            "description": "Only this stream.",
                            "enum": ["stdout", "stderr"],
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            ResourceCapability(
                "wait_ready",
                read,
                "Wait until a background command is ready: its output contains some text "
                "and/or a local port accepts connections",
                scope="process.job.ready",
                category="jobs",
                params={
                    "properties": {
                        "job_id": _JOB,
                        "log_contains": {
                            "type": "string",
                            "description": "Text that appears in the output when ready.",
                        },
                        "port": {
                            "type": "integer",
                            "description": "Local (loopback) port that must accept connections.",
                            "minimum": 1,
                            "maximum": 65535,
                        },
                        "timeout": {
                            "type": "integer",
                            "description": f"Seconds to wait (default 15, at most {int(_MAX_WAIT_S)}).",
                            "minimum": 1,
                            "maximum": int(_MAX_WAIT_S),
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            ResourceCapability(
                "stop",
                ResourcePermission.WRITE,
                "Stop a background command you started (and everything it spawned)",
                scope="process.job.stop",
                category="jobs",
                params={"properties": {"job_id": _JOB}, "required": ["job_id"]},
            ),
        ]

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        job = (params or {}).get("job_id")
        return f"process {action} {job}".strip() if job else f"process {action}"

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(
                f"Unsupported action '{action}'.",
                action=action,
                code=ErrorCode.UNSUPPORTED_OPERATION,
            )
        return await handler(params)

    def _visible_jobs(self) -> list[JobRecord]:
        jobs = [j for j in self._registry.all_jobs() if j.kind == "shell" and j.owner == "agent"]
        return sorted(jobs, key=lambda j: j.job_id)

    def _lookup(
        self, params: dict[str, Any], action: str
    ) -> tuple[JobRecord | None, ResourceResult | None]:
        raw = params.get("job_id")
        if not isinstance(raw, str) or not _JOB_ID.match(raw.strip()):
            return None, ResourceResult.failure(
                "job_id must look like 'job-0001'.",
                action=action,
                code=ErrorCode.VALIDATION_FAILED,
            )
        job = self._registry.get(raw.strip())
        if job is None or job.kind != "shell" or job.owner != "agent":
            # Identical answer for "missing" and "not yours": no probing of other jobs.
            return None, ResourceResult.failure(
                f"No background job '{raw.strip()}' started by this session.",
                action=action,
                code=ErrorCode.NOT_FOUND,
                hint="Use the list action to see the jobs you started.",
            )
        return job, None

    @staticmethod
    def _summary(job: JobRecord) -> dict[str, Any]:
        from velune.core.task_registry import JobStatus

        running = job.status in (JobStatus.PENDING, JobStatus.RUNNING)
        end = time.monotonic() if running else (job.completed_at or time.monotonic())
        info: dict[str, Any] = {
            "job_id": job.job_id,
            "command": redact_secrets(job.command or job.name)[:200],
            "status": job.status.value,
            "running": running,
            "seconds": round(max(0.0, end - job.submitted_at), 1),
        }
        if job.exit_code is not None:
            info["exit_code"] = job.exit_code
        if job.ready is not None:
            info["ready"] = job.ready
        if job.log is not None:
            info["log_lines"] = job.log.total_lines
        if job.error:
            info["error"] = redact_secrets(str(job.error))[:200]
        return info

    async def _do_list(self, params: dict[str, Any]) -> ResourceResult:
        jobs = [self._summary(j) for j in self._visible_jobs()]
        return ResourceResult.success("list", {"jobs": jobs, "count": len(jobs)})

    async def _do_status(self, params: dict[str, Any]) -> ResourceResult:
        job, error = self._lookup(params, "status")
        return error or ResourceResult.success("status", self._summary(job))

    async def _do_logs(self, params: dict[str, Any]) -> ResourceResult:
        job, error = self._lookup(params, "logs")
        if error:
            return error
        try:
            tail = int(params.get("tail", _DEFAULT_TAIL))
        except (TypeError, ValueError):
            return ResourceResult.failure(
                "tail must be an integer.", action="logs", code=ErrorCode.VALIDATION_FAILED
            )
        tail = max(1, min(tail, _MAX_LOG_LINES))
        stream = params.get("stream")
        if stream not in (None, "stdout", "stderr"):
            return ResourceResult.failure(
                "stream must be 'stdout' or 'stderr'.",
                action="logs",
                code=ErrorCode.VALIDATION_FAILED,
            )
        lines = self._registry.logs(job.job_id, tail, stream) or []
        data: dict[str, Any] = {
            "job_id": job.job_id,
            "lines": [line[:_MAX_LINE_CHARS] for line in lines],
        }
        if job.log is not None and job.log.dropped:
            data["older_lines_dropped"] = job.log.dropped
        return ResourceResult.success("logs", data)

    async def _do_wait_ready(self, params: dict[str, Any]) -> ResourceResult:
        from velune.execution.processes import wait_until_ready

        job, error = self._lookup(params, "wait_ready")
        if error:
            return error
        try:
            timeout = float(params.get("timeout", 15))
            port = int(params["port"]) if params.get("port") is not None else None
        except (TypeError, ValueError):
            return ResourceResult.failure(
                "port and timeout must be numbers.",
                action="wait_ready",
                code=ErrorCode.VALIDATION_FAILED,
            )
        text = params.get("log_contains")
        pattern = re.escape(str(text)) if text else None  # literal text: no ReDoS
        result = await wait_until_ready(
            self._registry,
            job.job_id,
            port=port,
            log_pattern=pattern,
            timeout=max(1.0, min(timeout, _MAX_WAIT_S)),
        )
        return ResourceResult.success(
            "wait_ready",
            {
                "job_id": job.job_id,
                "ready": result.ready,
                "reason": result.reason,
                "waited_seconds": result.waited_s,
            },
        )

    async def _do_stop(self, params: dict[str, Any]) -> ResourceResult:
        from velune.core.task_registry import JobStatus
        from velune.execution.processes import terminate_job

        job, error = self._lookup(params, "stop")
        if error:
            return error
        if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            result = ResourceResult.success(
                "stop", {**self._summary(job), "already_finished": True}
            )
            return result
        terminate_job(self._registry, job.job_id)
        result = ResourceResult.success("stop", {**self._summary(job), "stopped": True})
        result.resource_ids.append(job.job_id)
        return result
