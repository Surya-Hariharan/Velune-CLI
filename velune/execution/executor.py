"""Owns the command sandbox for a session: builds it and stops it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from velune.core.trace import TracedLogger
from velune.execution.sandbox import SubprocessSandbox

logger = TracedLogger("velune.execution.executor")

# Docker stop (10s grace) + rm; bounded so a wedged daemon cannot hang application exit.
SANDBOX_STOP_TIMEOUT_S = 20.0


def _build_sandbox(
    workspace_path: Path,
    config: Any | None,
    bus: Any | None,
) -> SubprocessSandbox:
    """Select and initialise the appropriate sandbox based on config.

    Tries DockerSandbox when execution.docker_sandbox=true; falls back to
    SubprocessSandbox if Docker is unavailable, logging a warning so the user
    knows which isolation tier is active.
    """
    use_docker = bool(
        config and getattr(getattr(config, "execution", None), "docker_sandbox", False)
    )
    if use_docker:
        try:
            from velune.execution.docker_sandbox import DockerSandbox

            image = getattr(getattr(config, "execution", None), "docker_image", "python:3.12-slim")
            sandbox = DockerSandbox(workspace_path, image=image, bus=bus)
            sandbox.start()
            logger.info(
                "Docker sandbox started (container=%s, image=%s)",
                sandbox.session_id,
                image,
            )
            return sandbox  # type: ignore[return-value]
        except Exception as exc:
            logger.warning(
                "Docker sandbox requested but unavailable (%s); "
                "falling back to subprocess sandbox.",
                exc,
            )

    allowed = config.execution.allowed_executables if config and config.execution else None
    return SubprocessSandbox(workspace_path, allowed_executables=allowed, bus=bus)


class ExecutionExecutor:
    """Builds the command sandbox and stops it when the session ends."""

    def __init__(
        self,
        workspace_path: Path,
        config: Any | None = None,
        bus: Any | None = None,
    ) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self._shutdown_started = False
        self.sandbox = _build_sandbox(self.workspace_path, config, bus)

    def _stop_sandbox(self) -> None:
        stop = getattr(self.sandbox, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception as exc:
            logger.warning("Stopping the command sandbox failed: %s", exc)

    async def shutdown(self, timeout: float = SANDBOX_STOP_TIMEOUT_S) -> None:
        """Stop the command sandbox this executor owns. Idempotent and bounded.

        The lifecycle coordinator calls this at application exit; without it a
        Docker-backed sandbox outlived the process. ``stop()`` is blocking
        (``docker stop`` + ``rm``), so it runs on a worker thread and the wait is
        capped: a hung daemon delays exit by at most *timeout*, not forever. If the
        caller is cancelled mid-stop the worker thread still finishes the stop.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        if not callable(getattr(self.sandbox, "stop", None)):
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self._stop_sandbox), timeout=timeout)
        except asyncio.TimeoutError:  # builtin TimeoutError only from 3.11
            logger.warning("Command sandbox did not stop within %.0fs; continuing exit", timeout)
