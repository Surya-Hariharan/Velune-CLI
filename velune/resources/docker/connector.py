"""Docker Desktop / Docker Engine connector.

Talks to the local Docker daemon through the ``docker`` CLI (so it works
identically on Windows/Docker Desktop, macOS, and Linux without a Python SDK
dependency). Read operations are auto-approved; container/image mutations and
``compose`` operations are permission-gated and never run automatically.

If Docker is not installed or the daemon is down, every method degrades to a
structured result — the connector never raises across its public surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from velune.resources.base import (
    DiscoveryHint,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)

logger = logging.getLogger("velune.resources.docker")

_DEFAULT_TIMEOUT = 20.0


class DockerConnector(ResourceConnector):
    """Connector for a local Docker daemon."""

    resource_id = "docker"
    display_name = "Docker"

    def __init__(self, name: str | None = None) -> None:
        super().__init__(name)
        self._state = ResourceState.DISCONNECTED
        self._error: str | None = None
        self._version: str | None = None
        self._has_compose: bool = False

    # ── Low-level CLI ────────────────────────────────────────────────────────

    @staticmethod
    def _docker_path() -> str | None:
        return shutil.which("docker")

    async def _run(self, args: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> ResourceResult:
        """Run ``docker <args>`` and capture output. Never raises."""
        docker = self._docker_path()
        if docker is None:
            return ResourceResult.failure(
                "Docker CLI not found on PATH.", action=args[0] if args else ""
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                docker,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return ResourceResult.failure(f"docker {args[0]} timed out after {timeout:.0f}s")
        except (OSError, ValueError) as exc:
            return ResourceResult.failure(f"Could not run docker: {exc}")

        stdout = out.decode("utf-8", "replace")
        stderr = err.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return ResourceResult.failure(stderr or f"docker exited with code {proc.returncode}")
        return ResourceResult.success(action=args[0] if args else "", data=stdout)

    @staticmethod
    def _parse_json_lines(text: str) -> list[dict[str, Any]]:
        """Parse ``docker ... --format '{{json .}}'`` line-delimited JSON output."""
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        if self._docker_path() is None:
            self._state = ResourceState.UNAVAILABLE
            self._error = "Docker is not installed (docker CLI not on PATH)."
            return ResourceResult.failure(self._error, action="connect")

        # `docker version` succeeds only when the daemon is reachable.
        self._state = ResourceState.CONNECTING
        result = await self._run(["version", "--format", "{{json .}}"], timeout=10.0)
        if not result.ok:
            self._state = ResourceState.ERROR
            self._error = "Docker daemon is not running or not reachable."
            return ResourceResult.failure(self._error, action="connect")

        try:
            info = json.loads(result.data)
            server = info.get("Server") or {}
            self._version = server.get("Version") or (info.get("Client") or {}).get("Version")
        except (ValueError, AttributeError):
            self._version = None

        # `docker compose version` presence detection (non-fatal).
        compose = await self._run(["compose", "version"], timeout=8.0)
        self._has_compose = compose.ok

        self._state = ResourceState.CONNECTED
        self._error = None
        return ResourceResult.success(
            action="connect",
            data={"version": self._version, "compose": self._has_compose},
        )

    async def disconnect(self) -> ResourceResult:
        # The Docker CLI is stateless; "disconnect" just drops our session flag.
        self._state = ResourceState.DISCONNECTED
        return ResourceResult.success(action="disconnect")

    def status(self) -> ResourceStatus:
        info: dict[str, Any] = {}
        if self._version:
            info["version"] = self._version
        info["compose"] = self._has_compose
        detail = f"Engine {self._version}" if self._version else ""
        return ResourceStatus(
            resource_id=self.resource_id,
            display_name=self.display_name,
            state=self._state,
            detail=detail,
            info=info,
            error=self._error,
        )

    async def discover(self) -> list[DiscoveryHint]:
        if self._docker_path() is None:
            return []
        # Cheap liveness probe; daemon-down still yields a hint that Docker is
        # installed (so the user can be told to start it), but marked as such.
        result = await self._run(["info", "--format", "{{.ServerVersion}}"], timeout=6.0)
        if result.ok:
            version = (result.data or "").strip()
            return [
                DiscoveryHint(
                    resource_id=self.resource_id,
                    display_name="Docker Desktop",
                    detail=f"Engine {version}" if version else "daemon running",
                    source="daemon",
                )
            ]
        return [
            DiscoveryHint(
                resource_id=self.resource_id,
                display_name="Docker (installed, daemon stopped)",
                detail="start Docker Desktop to connect",
                source="cli",
            )
        ]

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read = ResourcePermission.READ
        write = ResourcePermission.WRITE
        execute = ResourcePermission.EXECUTE
        return [
            ResourceCapability("ps", read, "List containers (running and stopped)"),
            ResourceCapability("images", read, "List local images"),
            ResourceCapability("inspect", read, "Inspect a container or image (JSON)"),
            ResourceCapability("logs", read, "Show a container's logs"),
            ResourceCapability("network_ls", read, "List Docker networks"),
            ResourceCapability("volume_ls", read, "List Docker volumes"),
            ResourceCapability("start", write, "Start an existing container"),
            ResourceCapability("stop", write, "Stop a running container"),
            ResourceCapability("restart", write, "Restart a container"),
            ResourceCapability("build", execute, "Build an image from a Dockerfile"),
            ResourceCapability("compose_up", execute, "docker compose up (detached)"),
            ResourceCapability("compose_down", execute, "docker compose down", destructive=True),
        ]

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        target = params.get("container") or params.get("image") or params.get("path") or ""
        return f"docker {action} {target}".strip()

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        if self._state is not ResourceState.CONNECTED and action not in {"ps", "images"}:
            # Allow a lazy connect for read actions so `/resource info docker`
            # style calls work, but surface a clear state otherwise.
            if self._docker_path() is None:
                return ResourceResult.failure("Docker is not installed.", action=action)

        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(f"Unsupported action '{action}'.", action=action)
        return await handler(params)

    # -- read actions --

    async def _do_ps(self, params: dict[str, Any]) -> ResourceResult:
        args = ["ps", "--format", "{{json .}}"]
        if params.get("all"):
            args.insert(1, "-a")
        result = await self._run(args)
        if result.ok:
            result.data = self._parse_json_lines(result.data)
        return result

    async def _do_images(self, params: dict[str, Any]) -> ResourceResult:
        result = await self._run(["images", "--format", "{{json .}}"])
        if result.ok:
            result.data = self._parse_json_lines(result.data)
        return result

    async def _do_inspect(self, params: dict[str, Any]) -> ResourceResult:
        target = params.get("container") or params.get("image") or params.get("target")
        if not target:
            return ResourceResult.failure(
                "inspect requires a 'container' or 'image' name.", action="inspect"
            )
        result = await self._run(["inspect", str(target)])
        if result.ok:
            try:
                result.data = json.loads(result.data)
            except ValueError:
                pass
        return result

    async def _do_logs(self, params: dict[str, Any]) -> ResourceResult:
        target = params.get("container")
        if not target:
            return ResourceResult.failure("logs requires a 'container' name.", action="logs")
        tail = str(params.get("tail", 100))
        return await self._run(["logs", "--tail", tail, str(target)])

    async def _do_network_ls(self, params: dict[str, Any]) -> ResourceResult:
        result = await self._run(["network", "ls", "--format", "{{json .}}"])
        if result.ok:
            result.data = self._parse_json_lines(result.data)
        return result

    async def _do_volume_ls(self, params: dict[str, Any]) -> ResourceResult:
        result = await self._run(["volume", "ls", "--format", "{{json .}}"])
        if result.ok:
            result.data = self._parse_json_lines(result.data)
        return result

    # -- write / execute actions (permission-gated by the manager) --

    async def _do_start(self, params: dict[str, Any]) -> ResourceResult:
        target = params.get("container")
        if not target:
            return ResourceResult.failure("start requires a 'container' name.", action="start")
        return await self._run(["start", str(target)])

    async def _do_stop(self, params: dict[str, Any]) -> ResourceResult:
        target = params.get("container")
        if not target:
            return ResourceResult.failure("stop requires a 'container' name.", action="stop")
        return await self._run(["stop", str(target)])

    async def _do_restart(self, params: dict[str, Any]) -> ResourceResult:
        target = params.get("container")
        if not target:
            return ResourceResult.failure("restart requires a 'container' name.", action="restart")
        return await self._run(["restart", str(target)])

    async def _do_build(self, params: dict[str, Any]) -> ResourceResult:
        path = str(params.get("path", "."))
        args = ["build", path]
        tag = params.get("tag")
        if tag:
            args = ["build", "-t", str(tag), path]
        return await self._run(args, timeout=600.0)

    async def _do_compose_up(self, params: dict[str, Any]) -> ResourceResult:
        if not self._has_compose:
            return ResourceResult.failure("docker compose is not available.", action="compose_up")
        args = ["compose"]
        if params.get("file"):
            args += ["-f", str(params["file"])]
        args += ["up", "-d"]
        return await self._run(args, timeout=600.0)

    async def _do_compose_down(self, params: dict[str, Any]) -> ResourceResult:
        if not self._has_compose:
            return ResourceResult.failure("docker compose is not available.", action="compose_down")
        args = ["compose"]
        if params.get("file"):
            args += ["-f", str(params["file"])]
        args += ["down"]
        if params.get("volumes"):
            args.append("-v")
        return await self._run(args, timeout=300.0)
