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
    ErrorCode,
    Lifecycle,
    ResourceCapability,
    ResourceConnector,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)

logger = logging.getLogger("velune.resources.docker")

_DEFAULT_TIMEOUT = 20.0

#: Parameters that become positional docker arguments (see ``execute``).
_ARGV_PARAMS = ("container", "image", "target", "path", "file", "tag", "tail")


class DockerConnector(ResourceConnector):
    """Connector for a local Docker daemon."""

    resource_id = "docker"
    display_name = "Docker"
    description = "Local Docker Engine: containers, images, logs, networks, volumes and Compose."
    keywords = (
        "docker",
        "container",
        "containers",
        "image",
        "images",
        "compose",
        "dockerfile",
        "volume",
        "volumes",
    )
    # The CLI is invoked per call; there is no session to keep open. A4 moves
    # this connector to the Docker SDK with a persistent client.
    lifecycle = Lifecycle.STATELESS

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
                "Docker CLI not found on PATH.",
                action=args[0] if args else "",
                code=ErrorCode.UNAVAILABLE,
                hint="Install Docker Desktop or Docker Engine.",
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
                return ResourceResult.failure(
                    f"docker {args[0]} timed out after {timeout:.0f}s", code=ErrorCode.TIMEOUT
                )
        except (OSError, ValueError) as exc:
            return ResourceResult.failure(
                f"Could not run docker: {exc}", code=ErrorCode.UNAVAILABLE
            )

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
        name = {"type": "string", "description": "Container name or id."}
        return [
            ResourceCapability(
                "ps",
                read,
                "List containers (running and stopped)",
                scope="docker.container.list",
                category="inspect",
                params={
                    "properties": {
                        "all": {"type": "boolean", "description": "Include stopped containers."}
                    }
                },
            ),
            ResourceCapability(
                "images",
                read,
                "List local images",
                scope="docker.image.list",
                category="inspect",
            ),
            ResourceCapability(
                "inspect",
                read,
                "Inspect a container or image (JSON)",
                scope="docker.inspect",
                category="inspect",
                params={
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "Container or image name or id.",
                        }
                    },
                    "required": ["target"],
                },
            ),
            ResourceCapability(
                "logs",
                read,
                "Show a container's recent logs",
                scope="docker.container.logs",
                category="inspect",
                params={
                    "properties": {
                        "container": name,
                        "tail": {
                            "type": "integer",
                            "description": "Number of lines from the end (default 100).",
                            "minimum": 1,
                            "maximum": 5000,
                        },
                    },
                    "required": ["container"],
                },
            ),
            ResourceCapability(
                "network_ls",
                read,
                "List Docker networks",
                scope="docker.network.list",
                category="inspect",
            ),
            ResourceCapability(
                "volume_ls",
                read,
                "List Docker volumes",
                scope="docker.volume.list",
                category="inspect",
            ),
            ResourceCapability(
                "start",
                write,
                "Start an existing container",
                scope="docker.container.start",
                category="containers",
                params={"properties": {"container": name}, "required": ["container"]},
            ),
            ResourceCapability(
                "stop",
                write,
                "Stop a running container",
                scope="docker.container.stop",
                category="containers",
                params={"properties": {"container": name}, "required": ["container"]},
            ),
            ResourceCapability(
                "restart",
                write,
                "Restart a container",
                scope="docker.container.restart",
                category="containers",
                params={"properties": {"container": name}, "required": ["container"]},
            ),
            ResourceCapability(
                "build",
                execute,
                "Build an image from a Dockerfile in the workspace",
                scope="docker.image.build",
                category="images",
                params={
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Build context directory inside the workspace (default '.').",
                        },
                        "tag": {"type": "string", "description": "Name:tag for the new image."},
                    }
                },
            ),
            ResourceCapability(
                "compose_up",
                execute,
                "docker compose up (detached) for a compose file in the workspace",
                scope="docker.compose.up",
                category="compose",
                params={
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Compose file inside the workspace (default: the standard names).",
                        }
                    }
                },
            ),
            ResourceCapability(
                "compose_down",
                execute,
                "docker compose down",
                destructive=True,
                scope="docker.compose.down",
                category="compose",
                params={
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Compose file inside the workspace.",
                        },
                        "volumes": {
                            "type": "boolean",
                            "description": "Also delete the project's volumes (data loss).",
                        },
                    }
                },
            ),
        ]

    async def probe_availability(self) -> tuple[bool, str]:
        """The daemon must be reachable — the CLI being installed is not enough."""
        hints = await self.discover()
        if not hints:
            return False, "Docker is not installed."
        if any(h.source == "daemon" for h in hints):
            return True, ""
        return False, "Docker is installed but its daemon is not running — start Docker Desktop."

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        target = params.get("container") or params.get("image") or params.get("path") or ""
        return f"docker {action} {target}".strip()

    # ── Ownership-aware authorization (Decision E) ───────────────────────────

    _LIFECYCLE_ACTIONS = frozenset({"start", "stop", "restart"})

    def describe_actions(self, action, params, *, ledger=None):
        """Raise risk for anything this task does not own.

        Starting, stopping or restarting a container is an ordinary (MEDIUM)
        change only for a container this session created. For every other
        container — the user's, another process's, or one whose origin is
        unknown — it becomes HIGH, so the gate asks a human in every mode,
        AUTO included. ``compose down -v`` additionally names the volume
        deletion it performs.

        ``compose up`` creates whatever the compose file describes, so the file
        is inspected (:mod:`~velune.resources.docker.compose_scan`): privileged
        containers, host namespaces, host bind mounts, the Docker socket,
        devices, and anything it cannot verify each add a HIGH-risk action with
        its own scope, so unattended AUTO mode never starts them.
        """
        from dataclasses import replace

        from velune.permissions.actions import Action, ActionType, Risk
        from velune.resources.ownership import Ownership, ResourceRef

        actions = super().describe_actions(action, params, ledger=ledger)
        params = params or {}
        if action in self._LIFECYCLE_ACTIONS and actions:
            container = str(params.get("container") or "")
            owned = (
                ledger is not None
                and bool(container)
                and ledger.ownership(ResourceRef("docker", "container", container))
                is Ownership.OWNED
            )
            if not owned:
                actions = [
                    replace(
                        a,
                        risk=Risk.HIGH,
                        reason=(
                            f"container '{container or '?'}' was not created by this task — "
                            "an existing resource is never assumed to be the task's own"
                        ),
                    )
                    for a in actions
                ]
        if action == "compose_up":
            from velune.resources.docker.compose_scan import compose_risks

            for risk in compose_risks(str(params["file"]) if params.get("file") else None):
                actions.append(
                    Action(
                        ActionType.INTEGRATION_EXECUTE,
                        f"{self.name}:compose_up",
                        f"the compose file {risk.reason}",
                        Risk.HIGH,
                        scope=risk.scope,
                    )
                )
        if action == "compose_down" and params.get("volumes"):
            actions.append(
                Action(
                    ActionType.INTEGRATION_DELETE,
                    f"{self.name}:compose_down",
                    "deletes the project's named volumes (data loss)",
                    Risk.HIGH,
                    scope="docker.volume.delete",
                )
            )
        return actions

    def ownership_effects(self, action, params, result):
        from velune.resources.ownership import ResourceRef

        params = params or {}
        if action == "build" and params.get("tag"):
            return [(ResourceRef("docker", "image", str(params["tag"])), True)]
        if action == "compose_up":
            project = str(params.get("file") or "compose")
            return [(ResourceRef("docker", "compose_project", project), True)]
        if action == "compose_down":
            project = str(params.get("file") or "compose")
            return [(ResourceRef("docker", "compose_project", project), False)]
        return []

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = params or {}
        # Names and paths go straight into the docker argv; one that starts with
        # "-" would be parsed by Docker as an option (``--help``, ``-a``), even
        # for read actions that run without asking.
        for key in _ARGV_PARAMS:
            value = params.get(key)
            if value is not None and str(value).lstrip().startswith("-"):
                return ResourceResult.failure(
                    f"'{key}' must not start with '-' (it would be read as a docker option).",
                    action=action,
                    code=ErrorCode.VALIDATION_FAILED,
                )
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
