"""Docker Engine connector (VAIR A4): the Docker SDK for Python, with Compose through the CLI.

Daemon access is host-level privilege, so this connector is built around three
rules:

1. **Everything goes through the manifest and the permission gate.** Each operation
   declares a tier and a scope; :meth:`describe_actions` (before anything runs)
   escalates it by what the *call* does — a privileged container, host networking,
   a bind mount outside the workspace, the Docker socket, a device, an exposed port,
   a remote daemon, or acting on something Velune did not create. The gate decides;
   discovery grants nothing.
2. **Ownership is proven, never inferred from a name** (see :mod:`.ownership`).
   Operations resolve a model-supplied name to the daemon's stable *id* first and
   then act on that id, so a name cannot be swapped between the check and the act.
   AUTO mode manages only objects this session created; everything else needs a
   person.
3. **No reflection.** There is no generic "call any SDK method" operation. Each
   operation validates its arguments (:mod:`.validation`), calls one fixed SDK
   method, and returns a curated, bounded, redacted view (:mod:`.curate`).

Compose still uses the ``docker compose`` CLI (the SDK has no Compose
implementation), with a forced project name and an argv that never carries
model-controlled option text. Everything else is native SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

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
from velune.resources.docker import curate
from velune.resources.docker import validation as v
from velune.resources.docker.engine import DockerEngine, EngineError
from velune.resources.docker.ownership import (
    DockerOwnership,
    DockerOwnershipStore,
    Facts,
)

logger = logging.getLogger("velune.resources.docker")

_DEFAULT_TIMEOUT = 20.0
_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none"})
_LIST_LIMIT = 500
_ASSESS_TTL = 1.5
#: Parameters that become positional docker (CLI) arguments; none may look like an option.
_ARGV_PARAMS = ("container", "image", "target", "path", "file", "tag", "tail", "project")
_LIFECYCLE = frozenset({"start", "stop", "restart", "pause", "unpause"})

_CONTAINER = {"type": "string", "description": "Container name or id."}
_PROJECT = {
    "type": "string",
    "description": "Compose project name (default: the compose file's directory name).",
}
_FILE = {
    "type": "string",
    "description": "Compose file inside the workspace (default: the standard names).",
}


@dataclass(slots=True)
class Assessment:
    """What a call would do to Docker, judged before it runs."""

    facts: Facts | None = None
    ownership: DockerOwnership = DockerOwnership.UNKNOWN
    reasons: list[str] = field(default_factory=list)
    #: ``(scope, reason, outside_workspace, secret)`` for each extra HIGH-risk action.
    extra: list[tuple[str, str, bool, bool]] = field(default_factory=list)
    high: bool = False
    escalate: tuple[ResourcePermission, bool] | None = None
    blocked: str = ""
    network: str = ""
    problem: str = ""
    at: float = field(default_factory=time.monotonic)


class DockerConnector(ResourceConnector):
    """Connector for a Docker Engine, through the Docker SDK for Python."""

    resource_id = "docker"
    display_name = "Docker"
    description = (
        "Local Docker Engine: containers, images, volumes, networks, logs, health and Compose, "
        "with ownership tracking so it only manages what it created."
    )
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
    # One long-lived SDK client, reused; the manager owns connect and disconnect.
    lifecycle = Lifecycle.PERSISTENT

    def __init__(
        self,
        name: str | None = None,
        *,
        config: Any = None,
        workspace: Path | str | None = None,
        engine: DockerEngine | None = None,
        store: DockerOwnershipStore | None = None,
        session_id: str = "",
        workspace_trusted: bool | None = None,
    ) -> None:
        super().__init__(name)
        from velune.kernel.config import DockerConfig

        try:
            if config is None:
                self.config = DockerConfig()
            elif isinstance(config, DockerConfig):
                self.config = config
            else:
                self.config = DockerConfig.model_validate(config)
        except Exception:
            self.config = DockerConfig()
        self.workspace = Path(workspace).resolve() if workspace else None
        if workspace_trusted is None:
            workspace_trusted = True
            if self.workspace is not None:
                try:
                    from velune.core.trust import is_trusted

                    workspace_trusted = bool(is_trusted(self.workspace))
                except Exception:
                    workspace_trusted = False
        self.engine = engine or DockerEngine(self.config, trust_config=bool(workspace_trusted))
        self.store = store or DockerOwnershipStore(
            session_id or _new_session_id(), self.workspace, path=self._store_path()
        )
        self._state = ResourceState.DISCONNECTED
        self._error: str | None = None
        self._has_compose = False
        self._bound = False
        self._connect_lock = asyncio.Lock()
        self._assessments: dict[str, Assessment] = {}
        self._authorized: dict[tuple[str, str], tuple[str, DockerOwnership]] = {}
        self._effects: dict[int, list[tuple[str, str, str, bool]]] = {}

    def _store_path(self) -> Path | None:
        if self.workspace is None:
            return None
        try:
            from velune.core.paths import workspace_storage_dir

            return workspace_storage_dir(self.workspace, create=False) / "docker-ownership.json"
        except Exception:
            return None

    # ── Low-level CLI (Compose and build only) ───────────────────────────────

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
                docker, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return ResourceResult.failure(
                    f"docker {args[0]} timed out after {timeout:.0f}s", code=ErrorCode.TIMEOUT
                )
            except asyncio.CancelledError:
                proc.kill()
                raise
        except (OSError, ValueError) as exc:
            return ResourceResult.failure(
                f"Could not run docker: {exc}", code=ErrorCode.UNAVAILABLE
            )
        stdout = out.decode("utf-8", "replace")
        stderr = err.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return ResourceResult.failure(
                stderr[-1000:] or f"docker exited with code {proc.returncode}"
            )
        return ResourceResult.success(action=args[0] if args else "", data=stdout)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> ResourceResult:
        self._state = ResourceState.CONNECTING
        try:
            info = await self.engine.connect()
            if not self._bound:
                await self.engine.call(self._bind_store, "reading daemon state", idempotent=True)
                self._bound = True
        except EngineError as exc:
            self._state = (
                ResourceState.UNAVAILABLE if "not installed" in exc.message else ResourceState.ERROR
            )
            self._error = exc.message
            return ResourceResult.failure(
                exc.message, action="connect", code=exc.code, hint=exc.hint
            )
        probe = (
            await self._run(["compose", "version"], timeout=8.0) if self._docker_path() else None
        )
        self._has_compose = bool(probe and probe.ok)
        self._state = ResourceState.CONNECTED
        self._error = None
        return ResourceResult.success(action="connect", data={**info, "compose": self._has_compose})

    def _bind_store(self, client: Any) -> None:
        """Bind ownership records to this daemon, snapshot what already exists, reconcile."""
        info = client.info()
        endpoint = self.engine.endpoint.describe() if self.engine.endpoint else ""
        self.store.bind_daemon(str(info.get("ID") or endpoint))
        live = self._list_facts(client)
        for kind, facts in live.items():
            unrecorded = [i for i in facts if self.store.record_for(kind, i) is None]
            self.store.remember_existing(kind, unrecorded)
        self.store.reconcile(live)

    def _list_facts(self, client: Any) -> dict[str, dict[str, Facts]]:
        out: dict[str, dict[str, Facts]] = {}
        try:
            out["container"] = {
                f.id: f
                for f in (
                    curate.container_facts(c.attrs) for c in client.containers.list(all=True)[:2000]
                )
            }
            out["volume"] = {
                f.id: f
                for f in (curate.volume_facts(x.attrs) for x in client.volumes.list()[:2000])
            }
            out["network"] = {
                f.id: f
                for f in (curate.network_facts(n.attrs) for n in client.networks.list()[:2000])
            }
            out["image"] = {
                f.id: f for f in (curate.image_facts(i.attrs) for i in client.images.list()[:2000])
            }
        except Exception:
            logger.debug("listing Docker objects for reconciliation failed", exc_info=True)
        return out

    async def disconnect(self) -> ResourceResult:
        await asyncio.to_thread(self.engine.close)
        self._bound = False
        self._state = ResourceState.DISCONNECTED
        return ResourceResult.success(action="disconnect")

    def status(self) -> ResourceStatus:
        info: dict[str, Any] = {"compose": self._has_compose}
        if self.engine.server_version:
            info["version"] = self.engine.server_version
            info["api"] = self.engine.api_version
            info["endpoint"] = self.engine.endpoint.describe() if self.engine.endpoint else ""
        detail = f"Engine {self.engine.server_version}" if self.engine.server_version else ""
        if self.engine.remote:
            detail += " (remote)"
        return ResourceStatus(
            resource_id=self.resource_id,
            display_name=self.display_name,
            state=self._state
            if self.engine.connected or self._state is not ResourceState.CONNECTED
            else ResourceState.DISCONNECTED,
            detail=detail,
            info=info,
            error=self._error,
        )

    async def health(self) -> bool:
        """Is the daemon still there? (A restart or quit shows up here.)"""
        alive = await self.engine.ping()
        if not alive and self._state is ResourceState.CONNECTED:
            self._state = ResourceState.ERROR
            self._error = "The Docker daemon stopped responding."
            self._bound = False
        return alive

    async def discover(self) -> list[DiscoveryHint]:
        try:
            info = await self.engine.connect()
        except EngineError as exc:
            if self._docker_path() is None and "SDK" not in exc.message:
                return []
            return [
                DiscoveryHint(
                    resource_id=self.resource_id,
                    display_name="Docker (installed, daemon stopped)",
                    detail="start Docker Desktop to connect",
                    source="cli",
                )
            ]
        return [
            DiscoveryHint(
                resource_id=self.resource_id,
                display_name="Docker Desktop",
                detail=f"Engine {info.get('version')}" if info.get("version") else "daemon running",
                source="daemon",
            )
        ]

    async def probe_availability(self) -> tuple[bool, str]:
        """The daemon must be reachable and the SDK installed — the CLI alone is not enough."""
        try:
            await self.engine.connect()
            return True, ""
        except EngineError as exc:
            hint = f" {exc.hint}" if exc.hint else ""
            return False, f"{exc.message}{hint}"[:240]

    # ── Capabilities ─────────────────────────────────────────────────────────

    def capabilities(self) -> list[ResourceCapability]:
        read, write = ResourcePermission.READ, ResourcePermission.WRITE
        execute, admin = ResourcePermission.EXECUTE, ResourcePermission.ADMIN

        def cap(action, tier, text, scope, category, params=None, *, destructive=False):
            return ResourceCapability(
                action,
                tier,
                text,
                destructive=destructive,
                scope=scope,
                category=category,
                params=params,
            )

        def needs(**props):
            return {"properties": props, "required": list(props)}

        timeout = {
            "type": "integer",
            "description": "Seconds to wait for the container to stop (default 10).",
            "minimum": 0,
            "maximum": 300,
        }
        tail = {
            "type": "integer",
            "description": "Lines from the end (default 100).",
            "minimum": 1,
            "maximum": 5000,
        }
        return [
            # ── inspect ──
            cap("info", read, "Docker daemon version and capacity", "docker.info", "inspect"),
            cap(
                "ps",
                read,
                "List containers (running and stopped) and whether Velune created each",
                "docker.container.list",
                "inspect",
                {
                    "properties": {
                        "all": {"type": "boolean", "description": "Include stopped containers."}
                    }
                },
            ),
            cap("images", read, "List local images", "docker.image.list", "inspect"),
            cap(
                "inspect",
                read,
                "Inspect a container or image (environment values are never shown)",
                "docker.inspect",
                "inspect",
                needs(target={"type": "string", "description": "Container or image name or id."}),
            ),
            cap(
                "image_inspect",
                read,
                "Inspect an image",
                "docker.image.inspect",
                "inspect",
                needs(image={"type": "string", "description": "Image reference or id."}),
            ),
            cap(
                "logs",
                read,
                "Recent output of a container (bounded and redacted)",
                "docker.container.logs",
                "inspect",
                {"properties": {"container": _CONTAINER, "tail": tail}, "required": ["container"]},
            ),
            cap(
                "health",
                read,
                "A container's health-check status",
                "docker.container.health",
                "inspect",
                needs(container=_CONTAINER),
            ),
            cap(
                "stats",
                read,
                "A one-shot CPU, memory and network reading for a container",
                "docker.container.stats",
                "inspect",
                needs(container=_CONTAINER),
            ),
            cap("network_ls", read, "List Docker networks", "docker.network.list", "inspect"),
            cap(
                "network_inspect",
                read,
                "Inspect a network and what is attached",
                "docker.network.inspect",
                "inspect",
                needs(network={"type": "string", "description": "Network name or id."}),
            ),
            cap(
                "volume_ls",
                read,
                "List Docker volumes and whether each is in use",
                "docker.volume.list",
                "inspect",
            ),
            cap(
                "volume_inspect",
                read,
                "Inspect a volume and which containers use it",
                "docker.volume.inspect",
                "inspect",
                needs(volume={"type": "string", "description": "Volume name."}),
            ),
            # ── containers ──
            cap(
                "container_create",
                write,
                "Create (not start) a container from a local image with validated options. "
                "Privileged mode, host networking, devices, host paths outside the workspace, the "
                "Docker socket and non-loopback ports each need a person's approval",
                "docker.container.create",
                "containers",
                {
                    "properties": {
                        "image": {
                            "type": "string",
                            "description": "Local image reference (pull it first).",
                        },
                        "name": {"type": "string", "description": "Container name."},
                        "command": {
                            "type": "string",
                            "description": "Command, split like a shell would (never run through a shell).",
                        },
                        "env": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "NAME=value items.",
                        },
                        "ports": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "[ip:]host:container[/tcp|udp]; the host side binds to 127.0.0.1 unless an ip is given.",
                        },
                        "volumes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "source:/container/path[:ro]; source is a named volume or a host path.",
                        },
                        "network": {"type": "string", "description": "Network to join."},
                        "restart_policy": {
                            "type": "string",
                            "description": "Restart policy.",
                            "enum": list(v.RESTART_POLICIES),
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "key=value labels.",
                        },
                        "memory": {
                            "type": "string",
                            "description": "Memory limit like 512m or 2g.",
                        },
                        "cpus": {
                            "type": "number",
                            "description": "CPU limit, e.g. 1.5.",
                            "minimum": 0.01,
                            "maximum": 256,
                        },
                        "privileged": {
                            "type": "boolean",
                            "description": "Privileged mode (always needs approval).",
                        },
                        "host_network": {
                            "type": "boolean",
                            "description": "Share the host network (always needs approval).",
                        },
                        "devices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Host /dev paths (always need approval).",
                        },
                        "cap_add": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Linux capabilities to add (always need approval).",
                        },
                    },
                    "required": ["image"],
                },
            ),
            cap(
                "start",
                write,
                "Start an existing container",
                "docker.container.start",
                "containers",
                needs(container=_CONTAINER),
            ),
            cap(
                "stop",
                write,
                "Stop a running container",
                "docker.container.stop",
                "containers",
                {
                    "properties": {"container": _CONTAINER, "timeout": timeout},
                    "required": ["container"],
                },
            ),
            cap(
                "restart",
                write,
                "Restart a container",
                "docker.container.restart",
                "containers",
                {
                    "properties": {"container": _CONTAINER, "timeout": timeout},
                    "required": ["container"],
                },
            ),
            cap(
                "pause",
                write,
                "Pause a running container",
                "docker.container.pause",
                "containers",
                needs(container=_CONTAINER),
            ),
            cap(
                "unpause",
                write,
                "Unpause a paused container",
                "docker.container.unpause",
                "containers",
                needs(container=_CONTAINER),
            ),
            cap(
                "container_remove",
                write,
                "Remove a container. A container Velune created is removed on approval rules for "
                "changes; any other container (or force, or its volumes) always needs a person",
                "docker.container.remove",
                "containers",
                {
                    "properties": {
                        "container": _CONTAINER,
                        "force": {"type": "boolean", "description": "Remove even if running."},
                        "volumes": {
                            "type": "boolean",
                            "description": "Also remove its anonymous volumes (data loss).",
                        },
                    },
                    "required": ["container"],
                },
            ),
            cap(
                "wait_ready",
                read,
                "Wait until a container is ready: healthy, its output contains some text, or running steadily",
                "docker.container.ready",
                "containers",
                {
                    "properties": {
                        "container": _CONTAINER,
                        "log_contains": {
                            "type": "string",
                            "description": "Text that appears in the output when ready.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Seconds to wait (default 30, at most 120).",
                            "minimum": 1,
                            "maximum": 120,
                        },
                    },
                    "required": ["container"],
                },
            ),
            # ── images ──
            cap(
                "image_pull",
                write,
                "Pull an image from a registry (a network operation; uses your docker login)",
                "docker.image.pull",
                "images",
                needs(image={"type": "string", "description": "Image reference, e.g. nginx:1.27."}),
            ),
            cap(
                "image_tag",
                write,
                "Add a tag to an image (refuses to move an existing tag)",
                "docker.image.tag",
                "images",
                needs(
                    image={"type": "string", "description": "Source image reference or id."},
                    repository={"type": "string", "description": "New repository name."},
                    tag={"type": "string", "description": "New tag."},
                ),
            ),
            cap(
                "image_remove",
                admin,
                "Remove an image (always needs a person)",
                "docker.image.remove",
                "images",
                {
                    "properties": {
                        "image": {"type": "string", "description": "Image reference or id."},
                        "force": {
                            "type": "boolean",
                            "description": "Remove even if containers use it.",
                        },
                    },
                    "required": ["image"],
                },
                destructive=True,
            ),
            cap(
                "build",
                execute,
                "Build an image from a Dockerfile in the workspace",
                "docker.image.build",
                "images",
                {
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Build context directory inside the workspace (default '.').",
                        },
                        "tag": {"type": "string", "description": "Name:tag for the new image."},
                    }
                },
            ),
            # ── volumes ──
            cap(
                "volume_create",
                write,
                "Create a named volume",
                "docker.volume.create",
                "volumes",
                needs(name={"type": "string", "description": "Volume name (must not exist yet)."}),
            ),
            cap(
                "volume_remove",
                admin,
                "Remove a volume (always needs a person; refused while in use)",
                "docker.volume.delete",
                "volumes",
                needs(name={"type": "string", "description": "Volume name."}),
                destructive=True,
            ),
            # ── networks ──
            cap(
                "network_create",
                write,
                "Create a network",
                "docker.network.create",
                "networks",
                {
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Network name (must not exist yet).",
                        },
                        "internal": {"type": "boolean", "description": "No outside access."},
                    },
                    "required": ["name"],
                },
            ),
            cap(
                "network_connect",
                write,
                "Attach a container to a network",
                "docker.network.connect",
                "networks",
                needs(
                    network={"type": "string", "description": "Network name or id."},
                    container=_CONTAINER,
                ),
            ),
            cap(
                "network_disconnect",
                write,
                "Detach a container from a network",
                "docker.network.disconnect",
                "networks",
                needs(
                    network={"type": "string", "description": "Network name or id."},
                    container=_CONTAINER,
                ),
            ),
            cap(
                "network_remove",
                write,
                "Remove a network. One Velune created and nothing uses is a change; "
                "anything else needs a person",
                "docker.network.delete",
                "networks",
                needs(network={"type": "string", "description": "Network name or id."}),
            ),
            # ── compose ──
            cap(
                "compose_config",
                read,
                "Inspect a Compose file: services and the risks it declares",
                "docker.compose.config",
                "compose",
                {"properties": {"file": _FILE}},
            ),
            cap(
                "compose_ps",
                read,
                "State of a Compose project's containers",
                "docker.compose.ps",
                "compose",
                {"properties": {"file": _FILE, "project": _PROJECT}},
            ),
            cap(
                "compose_logs",
                read,
                "Recent logs of a Compose project's service",
                "docker.compose.logs",
                "compose",
                {
                    "properties": {
                        "file": _FILE,
                        "project": _PROJECT,
                        "service": {"type": "string", "description": "Service name (default all)."},
                        "tail": tail,
                    }
                },
            ),
            cap(
                "compose_up",
                execute,
                "docker compose up (detached) for a compose file in the workspace",
                "docker.compose.up",
                "compose",
                {"properties": {"file": _FILE, "project": _PROJECT}},
            ),
            cap(
                "compose_stop",
                execute,
                "Stop a Compose project's containers without removing them",
                "docker.compose.stop",
                "compose",
                {"properties": {"file": _FILE, "project": _PROJECT}},
            ),
            cap(
                "compose_down",
                execute,
                "docker compose down; only a project Velune created can be taken down",
                "docker.compose.down",
                "compose",
                {
                    "properties": {
                        "file": _FILE,
                        "project": _PROJECT,
                        "volumes": {
                            "type": "boolean",
                            "description": "Also delete the project's volumes (data loss).",
                        },
                    }
                },
                destructive=True,
            ),
        ]

    def authorization_preview(self, action: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        target = (
            params.get("container")
            or params.get("image")
            or params.get("network")
            or params.get("name")
            or params.get("path")
            or params.get("file")
            or ""
        )
        extra = ""
        if action == "container_create":
            extra = " " + " ".join(
                f"{k}={params[k]}" for k in ("ports", "volumes") if params.get(k)
            )
        return redact_secrets(f"docker {action} {target}{extra}".strip())[:300]

    # ── Assessment: what is this call, and who owns what it touches? ─────────

    def _peek(self, kind: str, ref: str) -> Facts | None:
        """Look *ref* up on the daemon for a decision. None when it cannot be looked up.

        Synchronous and bounded by the client timeout; only when already connected
        (a decision never starts a connection). Failure means *unknown*, which is the
        most cautious classification.
        """
        if not self.engine.connected or not self._bound:
            return None
        try:
            return self.engine.call_sync(
                lambda c: self._facts(c, kind, ref), "looking up", idempotent=True
            )
        except EngineError:
            return None

    @staticmethod
    def _facts(client: Any, kind: str, ref: str) -> Facts | None:
        try:
            if kind == "container":
                return curate.container_facts(client.containers.get(ref).attrs)
            if kind == "image":
                return curate.image_facts(client.images.get(ref).attrs)
            if kind == "volume":
                return curate.volume_facts(client.volumes.get(ref).attrs)
            return curate.network_facts(client.networks.get(ref).attrs)
        except Exception as exc:
            if "NotFound" in {c.__name__ for c in type(exc).__mro__}:
                return None
            raise

    def _assess(self, action: str, params: dict[str, Any] | None) -> Assessment:
        key = f"{action}:{json.dumps(params or {}, sort_keys=True, default=str)}"
        cached = self._assessments.get(key)
        if cached is not None and time.monotonic() - cached.at < _ASSESS_TTL:
            return cached
        try:
            result = self._assess_uncached(action, params or {})
        except v.DockerValidationError as exc:
            result = Assessment(problem=str(exc))  # execution will refuse it; nothing runs
        except Exception:
            logger.debug("assessing %s failed", action, exc_info=True)
            result = Assessment(high=True, reasons=["the call could not be assessed"])
        if len(self._assessments) > 64:
            self._assessments.clear()
        self._assessments[key] = result
        return result

    def _assess_uncached(self, action: str, p: dict[str, Any]) -> Assessment:
        a = Assessment()
        mutating = action in _LIFECYCLE | {
            "container_create",
            "container_remove",
            "image_pull",
            "image_tag",
            "image_remove",
            "volume_create",
            "volume_remove",
            "network_create",
            "network_connect",
            "network_disconnect",
            "network_remove",
        }
        if mutating and self.engine.remote:
            a.high = True
            a.reasons.append("the daemon is remote")
        if action in _LIFECYCLE:
            ref = v.container_ref(p.get("container"))
            a.facts = self._peek("container", ref)
            a.ownership = self.store.classify(a.facts)
            if not a.ownership.trusted:
                a.high = True
                a.reasons.append(self._not_owned(ref, a.ownership))
        elif action == "container_remove":
            ref = v.container_ref(p.get("container"))
            a.facts = self._peek("container", ref)
            a.ownership = self.store.classify(a.facts)
            owned = a.ownership.trusted and not p.get("force") and not p.get("volumes")
            if not owned:
                a.escalate = (ResourcePermission.ADMIN, True)
                a.reasons.append(
                    self._not_owned(ref, a.ownership)
                    if not a.ownership.trusted
                    else "removal was forced or includes the container's volumes"
                )
        elif action == "container_create":
            self._assess_create(a, p)
        elif action in ("image_pull",):
            ref = v.image_ref(p.get("image"))
            a.network = v.registry_of(ref)
        elif action == "image_tag":
            _image_or_id(p.get("image"))
            v.image_ref(f"{p.get('repository', '')}:{p.get('tag', '')}", "repository:tag")
        elif action == "image_remove":
            ref = _image_or_id(p.get("image"))
            a.facts = self._peek("image", ref)
            a.ownership = self.store.classify(a.facts)
            if not a.ownership.trusted:
                a.reasons.append(self._not_owned(ref, a.ownership))
        elif action == "volume_remove":
            ref = v.resource_name(p.get("name"), "volume")
            a.facts = self._peek("volume", ref)
            a.ownership = self.store.classify(a.facts)
            if not a.ownership.trusted:
                a.reasons.append(self._not_owned(ref, a.ownership))
        elif action == "network_remove":
            ref = v.resource_name(p.get("network"), "network")
            if ref in _BUILTIN_NETWORKS:
                a.blocked = f"'{ref}' is a built-in Docker network"
            a.facts = self._peek("network", ref)
            a.ownership = self.store.classify(a.facts)
            in_use = self._network_in_use(ref)
            if not a.ownership.trusted or in_use is not False:
                a.escalate = (ResourcePermission.ADMIN, True)
                a.reasons.append(
                    self._not_owned(ref, a.ownership)
                    if not a.ownership.trusted
                    else "the network is in use"
                )
        elif action in ("network_connect", "network_disconnect"):
            net = v.resource_name(p.get("network"), "network")
            ctr = v.container_ref(p.get("container"))
            owners = (
                self.store.classify(self._peek("network", net)),
                self.store.classify(self._peek("container", ctr)),
            )
            if not all(o.trusted for o in owners):
                a.high = True
                a.reasons.append("it changes a network or container Velune did not create")
        elif action in ("network_create", "volume_create"):
            v.resource_name(p.get("name"), "name")
        elif action in ("compose_stop", "compose_ps", "compose_logs"):
            self._assess_compose_project(a, p, action == "compose_stop")
        elif action == "build":
            tag = p.get("tag")
            if tag:
                v.image_ref(tag, "tag")
        return a

    def _network_in_use(self, ref: str) -> bool | None:
        if not self.engine.connected:
            return None
        try:
            attrs = self.engine.call_sync(
                lambda c: c.networks.get(ref).attrs, "inspecting a network", idempotent=True
            )
            return bool(attrs.get("Containers"))
        except EngineError:
            return None

    @staticmethod
    def _not_owned(ref: str, ownership: DockerOwnership) -> str:
        what = {
            DockerOwnership.PREVIOUS: "created by Velune in an earlier session",
            DockerOwnership.PREEXISTING: "already there before this session",
            DockerOwnership.EXTERNAL: "claims Velune labels but has no ownership record",
        }.get(ownership, "of unknown origin")
        return (
            f"'{ref}' was not created by this task ({what}) — an existing resource is never "
            "assumed to be the task's own"
        )

    def _assess_create(self, a: Assessment, p: dict[str, Any]) -> None:
        v.image_ref(p.get("image"))
        if p.get("name"):
            v.container_ref(p["name"], "name")
        if p.get("command"):
            v.command_args(p["command"])
        v.env_pairs(p.get("env"))
        v.label_pairs(p.get("labels"))
        if p.get("memory"):
            v.memory_bytes(p["memory"])
        if p.get("cpus") is not None:
            v.cpu_nanos(p["cpus"])
        if p.get("network"):
            v.resource_name(p["network"], "network")
        if p.get("restart_policy") and p["restart_policy"] not in v.RESTART_POLICIES:
            raise v.DockerValidationError(
                "restart_policy is not one of: " + ", ".join(v.RESTART_POLICIES)
            )
        for binding in v.port_bindings(p.get("ports")):
            if binding.exposed:
                a.extra.append(
                    (
                        "docker.network.exposed",
                        f"publishes port {binding.host_port} beyond loopback ({binding.host_ip})",
                        False,
                        False,
                    )
                )
        for mount in v.mount_specs(p.get("volumes"), self.workspace):
            if mount.kind != "bind":
                continue
            if mount.docker_socket:
                a.extra.append(
                    (
                        "docker.mount.host",
                        "mounts the Docker socket (root-equivalent on the host)",
                        False,
                        False,
                    )
                )
            elif not mount.inside_workspace:
                a.extra.append(
                    (
                        "docker.mount.host",
                        "bind-mounts a host path outside the workspace",
                        True,
                        False,
                    )
                )
            elif mount.secret or not self.config.workspace_mounts:
                reason = (
                    "bind-mounts a secret or protected workspace path"
                    if mount.secret
                    else "workspace bind mounts need approval"
                )
                a.extra.append(("docker.mount.host", reason, False, mount.secret))
        if p.get("privileged"):
            a.extra.append(
                ("docker.container.privileged", "runs a privileged container", False, False)
            )
        if p.get("cap_add"):
            v.capabilities(p["cap_add"])
            a.extra.append(("docker.container.privileged", "adds Linux capabilities", False, False))
        if p.get("host_network"):
            a.extra.append(("docker.network.host", "joins the host's network stack", False, False))
        if p.get("devices"):
            v.device_specs(p["devices"])
            a.extra.append(
                ("docker.device.access", "hands host devices to the container", False, False)
            )

    def _assess_compose_project(self, a: Assessment, p: dict[str, Any], mutating: bool) -> None:
        project = self._project_name(p)
        if not mutating:
            return
        members = self._project_members(project)
        if members is None or not members:
            a.high = True
            a.reasons.append(f"compose project '{project}' could not be checked for ownership")
            return
        bad = [m for m in members if not self.store.classify(m).trusted]
        if bad:
            a.high = True
            a.reasons.append(
                f"compose project '{project}' includes containers this task did not create"
            )

    # ── Authorization hooks ──────────────────────────────────────────────────

    def effective_permission(
        self, action: str, params: dict[str, Any] | None
    ) -> tuple[ResourcePermission, bool] | None:
        if action in _LIFECYCLE | {
            "container_remove",
            "network_remove",
            "container_create",
            "image_pull",
            "image_tag",
            "volume_create",
            "network_create",
            "network_connect",
            "network_disconnect",
            "volume_remove",
            "image_remove",
            "compose_stop",
        }:
            return self._assess(action, params).escalate
        return None

    def describe_actions(self, action, params, *, ledger=None):
        """Raise risk for what the call does and for what it touches (see the module doc).

        Starts from the declared tier, then adds: HIGH risk and a reason for anything the
        task does not own or a remote daemon; one extra HIGH action with its own scope for
        each privileged option, host mount, device, exposed port or risky Compose setting;
        a blocked flag for built-in networks; and a network action for a registry pull.
        """
        from velune.permissions.actions import Action, ActionType, Risk

        actions = super().describe_actions(action, params, ledger=ledger)
        params = params or {}
        a = self._assess(action, params)
        if a.high or a.reasons:
            reason = "; ".join(a.reasons)
            actions = [
                replace(
                    x,
                    risk=Risk.HIGH if a.high else x.risk,
                    reason=reason or x.reason,
                    blocked=bool(a.blocked) or x.blocked,
                )
                for x in actions
            ]
        elif a.blocked:
            actions = [replace(x, blocked=True, reason=a.blocked) for x in actions]
        for scope, reason, outside, secret in a.extra:
            actions.append(
                Action(
                    ActionType.INTEGRATION_EXECUTE,
                    f"{self.name}:{action}",
                    f"the container {reason}",
                    Risk.HIGH,
                    outside_workspace=outside,
                    secret=secret,
                    scope=scope,
                )
            )
        if a.network:
            actions.append(
                Action(
                    ActionType.NETWORK, a.network, f"pulls an image from {a.network}", Risk.MEDIUM
                )
            )
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
        if actions and a.facts is not None and action in _LIFECYCLE | {"container_remove"}:
            self._authorized[(action, str(params.get("container")))] = (a.facts.id, a.ownership)
        return actions

    def ownership_effects(self, action, params, result):
        """Ledger effects recorded by the handler that created or removed an object."""
        from velune.resources.ownership import ResourceRef

        out = []
        for kind, ident, _name, created in self._effects.pop(id(result), []):
            out.append((ResourceRef("docker", kind, ident), created))
        return out

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, action: str, params: dict[str, Any] | None = None) -> ResourceResult:
        params = dict(params or {})
        # Values that reach a docker argv must never be read as an option.
        for key in _ARGV_PARAMS:
            value = params.get(key)
            if value is not None and str(value).lstrip().startswith("-"):
                return ResourceResult.failure(
                    f"'{key}' must not start with '-' (it would be read as a docker option).",
                    action=action,
                    code=ErrorCode.VALIDATION_FAILED,
                )
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ResourceResult.failure(
                f"Unsupported action '{action}'.",
                action=action,
                code=ErrorCode.UNSUPPORTED_OPERATION,
            )
        if not (self.engine.connected and self._bound):
            # The model-facing tools connect first; a direct call (``/resource``) must too,
            # so ownership records are bound to the daemon before anything is judged.
            async with self._connect_lock:
                if not (self.engine.connected and self._bound):
                    opened = await self.connect()
                    if not opened.ok:
                        opened.action = action
                        return opened
        self._assessments.clear()  # a decision is never reused across a change
        try:
            result = await handler(params)
        except v.DockerValidationError as exc:
            return ResourceResult.failure(str(exc), action=action, code=ErrorCode.VALIDATION_FAILED)
        except EngineError as exc:
            if exc.code is ErrorCode.UNAVAILABLE:
                self._state, self._error = ResourceState.ERROR, exc.message
                self._bound = False
            return ResourceResult.failure(exc.message, action=action, code=exc.code, hint=exc.hint)
        self._assessments.clear()
        return result

    def _limit_chars(self) -> int:
        return int(self.config.max_output_chars)

    def _effect(
        self, result: ResourceResult, kind: str, ident: str, name: str, created: bool
    ) -> None:
        self._effects.setdefault(id(result), []).append((kind, ident, name, created))
        if ident not in result.resource_ids:
            result.resource_ids.append(ident[:64])

    def _owner(self, facts: Facts | None) -> str:
        return self.store.classify(facts).value

    # -- inspect --

    async def _do_info(self, p: dict[str, Any]) -> ResourceResult:
        info = await self.engine.call(lambda c: c.info(), "reading daemon info", idempotent=True)
        data = {
            "version": self.engine.server_version,
            "api_version": self.engine.api_version,
            "os": info.get("OperatingSystem"),
            "architecture": info.get("Architecture"),
            "containers": info.get("Containers"),
            "containers_running": info.get("ContainersRunning"),
            "images": info.get("Images"),
            "cpus": info.get("NCPU"),
            "memory_mb": round(int(info.get("MemTotal") or 0) / 1_000_000),
            "remote": self.engine.remote,
            "endpoint": self.engine.endpoint.describe() if self.engine.endpoint else "",
            "compose": self._has_compose,
        }
        return ResourceResult.success("info", data)

    async def _do_ps(self, p: dict[str, Any]) -> ResourceResult:
        def work(c: Any) -> list[dict[str, Any]]:
            rows = []
            for ctr in c.containers.list(all=bool(p.get("all")))[:_LIST_LIMIT]:
                facts = curate.container_facts(ctr.attrs)
                rows.append(curate.container_row(ctr.attrs, self._owner(facts)))
            return rows

        rows = await self.engine.call(work, "listing containers", idempotent=True)
        return ResourceResult.success("ps", rows)

    async def _do_images(self, p: dict[str, Any]) -> ResourceResult:
        def work(c: Any) -> list[dict[str, Any]]:
            return [curate.image_row(i.attrs) for i in c.images.list()[:_LIST_LIMIT]]

        return ResourceResult.success(
            "images", await self.engine.call(work, "listing images", idempotent=True)
        )

    async def _do_inspect(self, p: dict[str, Any]) -> ResourceResult:
        target = p.get("target") or p.get("container") or p.get("image")
        if not target:
            return ResourceResult.failure(
                "inspect requires a 'target' (container or image).",
                action="inspect",
                code=ErrorCode.VALIDATION_FAILED,
            )
        ref = (
            v.image_ref(target)
            if ("/" in str(target) or ":" in str(target))
            else v.container_ref(target, "target")
        )

        def work(c: Any) -> dict[str, Any]:
            try:
                ctr = c.containers.get(ref)
                return curate.container_view(
                    ctr.attrs, self._owner(curate.container_facts(ctr.attrs))
                )
            except Exception as exc:
                if "NotFound" not in {k.__name__ for k in type(exc).__mro__}:
                    raise
            img = c.images.get(ref)
            return curate.image_view(img.attrs, self._owner(curate.image_facts(img.attrs)))

        return ResourceResult.success(
            "inspect", await self.engine.call(work, "inspecting", idempotent=True)
        )

    async def _do_image_inspect(self, p: dict[str, Any]) -> ResourceResult:
        ref = _image_or_id(p.get("image"))

        def work(c: Any) -> dict[str, Any]:
            img = c.images.get(ref)
            return curate.image_view(img.attrs, self._owner(curate.image_facts(img.attrs)))

        return ResourceResult.success(
            "image_inspect", await self.engine.call(work, "inspecting an image", idempotent=True)
        )

    async def _do_logs(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.container_ref(p.get("container"))
        tail = max(1, min(int(p.get("tail") or 100), self.config.max_log_lines))

        def work(c: Any) -> bytes:
            return c.containers.get(ref).logs(tail=tail, stdout=True, stderr=True, timestamps=False)

        raw = await self.engine.call(work, "reading logs", idempotent=True)
        data = curate.bounded_text(raw, max_chars=self._limit_chars(), tail_lines=tail)
        return ResourceResult.success("logs", {"container": ref, **data})

    async def _do_health(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.container_ref(p.get("container"))

        def work(c: Any) -> dict[str, Any]:
            state = c.containers.get(ref).attrs.get("State") or {}
            health = state.get("Health")
            out: dict[str, Any] = {
                "container": ref,
                "status": state.get("Status"),
                "running": bool(state.get("Running")),
            }
            if not health:
                out["health"] = "none"
                out["note"] = "this container defines no health check"
                return out
            out["health"] = health.get("Status")
            out["failing_streak"] = health.get("FailingStreak")
            out["recent"] = [
                {
                    "exit": e.get("ExitCode"),
                    "output": redact_secrets(str(e.get("Output", "")))[:200],
                }
                for e in (health.get("Log") or [])[-3:]
            ]
            return out

        return ResourceResult.success(
            "health", await self.engine.call(work, "reading health", idempotent=True)
        )

    async def _do_stats(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.container_ref(p.get("container"))
        raw = await self.engine.call(
            lambda c: c.containers.get(ref).stats(stream=False), "reading stats", idempotent=True
        )
        return ResourceResult.success("stats", {"container": ref, **curate.stats_view(raw)})

    async def _do_network_ls(self, p: dict[str, Any]) -> ResourceResult:
        def work(c: Any) -> list[dict[str, Any]]:
            return [
                curate.network_row(n.attrs, self._owner(curate.network_facts(n.attrs)))
                for n in c.networks.list()[:_LIST_LIMIT]
            ]

        return ResourceResult.success(
            "network_ls", await self.engine.call(work, "listing networks", idempotent=True)
        )

    async def _do_network_inspect(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.resource_name(p.get("network"), "network")

        def work(c: Any) -> dict[str, Any]:
            attrs = c.networks.get(ref).attrs
            return curate.network_row(attrs, self._owner(curate.network_facts(attrs)))

        return ResourceResult.success(
            "network_inspect", await self.engine.call(work, "inspecting a network", idempotent=True)
        )

    def _volume_users(self, client: Any) -> dict[str, list[str]]:
        users: dict[str, list[str]] = {}
        for ctr in client.containers.list(all=True)[:_LIST_LIMIT]:
            name = str(ctr.attrs.get("Name", "")).lstrip("/")
            for m in ctr.attrs.get("Mounts") or []:
                if m.get("Type") == "volume" and m.get("Name"):
                    users.setdefault(str(m["Name"]), []).append(name)
        return users

    async def _do_volume_ls(self, p: dict[str, Any]) -> ResourceResult:
        def work(c: Any) -> list[dict[str, Any]]:
            users = self._volume_users(c)
            return [
                curate.volume_row(
                    x.attrs,
                    self._owner(curate.volume_facts(x.attrs)),
                    users.get(str(x.attrs.get("Name")), []),
                )
                for x in c.volumes.list()[:_LIST_LIMIT]
            ]

        return ResourceResult.success(
            "volume_ls", await self.engine.call(work, "listing volumes", idempotent=True)
        )

    async def _do_volume_inspect(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.resource_name(p.get("volume"), "volume")

        def work(c: Any) -> dict[str, Any]:
            attrs = c.volumes.get(ref).attrs
            return curate.volume_row(
                attrs, self._owner(curate.volume_facts(attrs)), self._volume_users(c).get(ref, [])
            )

        return ResourceResult.success(
            "volume_inspect", await self.engine.call(work, "inspecting a volume", idempotent=True)
        )

    # -- containers --

    def _verify_unchanged(self, action: str, ref: str, facts: Facts | None) -> None:
        """Refuse when the object is not the one that was authorized (a name was re-pointed)."""
        approved = self._authorized.get((action, ref))
        if approved is None or facts is None:
            return
        old_id, old_owner = approved
        if old_owner.trusted and (facts.id != old_id or not self.store.classify(facts).trusted):
            raise EngineError(
                ErrorCode.AUTHORIZATION_DENIED,
                f"'{ref}' no longer refers to the container that was authorized; nothing was changed.",
                "Run the operation again.",
            )

    async def _lifecycle(self, action: str, p: dict[str, Any], op: Any) -> ResourceResult:
        ref = v.container_ref(p.get("container"))

        def work(c: Any) -> dict[str, Any]:
            facts = self._facts(c, "container", ref)
            if facts is None:
                raise EngineError(ErrorCode.NOT_FOUND, f"No container '{ref}'.")
            self._verify_unchanged(action, ref, facts)
            ctr = c.containers.get(facts.id)  # act on the id that was judged, not the name
            op(ctr)
            ctr.reload()
            return curate.container_row(ctr.attrs, self._owner(curate.container_facts(ctr.attrs)))

        data = await self.engine.call(work, f"{action} of a container")
        result = ResourceResult.success(action, data)
        result.resource_ids.append(str(data.get("id", "")))
        return result

    async def _do_start(self, p: dict[str, Any]) -> ResourceResult:
        return await self._lifecycle("start", p, lambda ctr: ctr.start())

    async def _do_stop(self, p: dict[str, Any]) -> ResourceResult:
        t = int(p.get("timeout", 10))
        return await self._lifecycle("stop", p, lambda ctr: ctr.stop(timeout=t))

    async def _do_restart(self, p: dict[str, Any]) -> ResourceResult:
        t = int(p.get("timeout", 10))
        return await self._lifecycle("restart", p, lambda ctr: ctr.restart(timeout=t))

    async def _do_pause(self, p: dict[str, Any]) -> ResourceResult:
        return await self._lifecycle("pause", p, lambda ctr: ctr.pause())

    async def _do_unpause(self, p: dict[str, Any]) -> ResourceResult:
        return await self._lifecycle("unpause", p, lambda ctr: ctr.unpause())

    async def _do_container_remove(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.container_ref(p.get("container"))
        force, volumes = bool(p.get("force")), bool(p.get("volumes"))

        def work(c: Any) -> Facts:
            facts = self._facts(c, "container", ref)
            if facts is None:
                raise EngineError(ErrorCode.NOT_FOUND, f"No container '{ref}'.")
            self._verify_unchanged("container_remove", ref, facts)
            c.containers.get(facts.id).remove(force=force, v=volumes)
            return facts

        facts = await self.engine.call(work, "removing a container")
        self.store.release("container", facts.id)
        result = ResourceResult.success(
            "container_remove", {"removed": facts.name or ref, "id": curate.short_id(facts.id)}
        )
        self._effect(result, "container", facts.id, facts.name, False)
        return result

    async def _do_container_create(self, p: dict[str, Any]) -> ResourceResult:
        image = v.image_ref(p.get("image"))
        name = v.container_ref(p["name"], "name") if p.get("name") else None
        user_labels = v.label_pairs(p.get("labels"))
        ports = v.port_bindings(p.get("ports"))
        mounts = v.mount_specs(p.get("volumes"), self.workspace)
        kwargs: dict[str, Any] = {
            "image": image,
            "command": v.command_args(p["command"]) if p.get("command") else None,
            "environment": v.env_pairs(p.get("env")) or None,
            "name": name,
            "labels": {**user_labels, **self.store.labels()},
            "security_opt": ["no-new-privileges:true"],
        }
        if ports:
            kwargs["ports"] = {b.key(): (b.host_ip, b.host_port) for b in ports}
        if mounts:
            kwargs["volumes"] = {
                m.source: {"bind": m.target, "mode": "ro" if m.read_only else "rw"} for m in mounts
            }
        if p.get("network"):
            kwargs["network"] = v.resource_name(p["network"], "network")
        if p.get("restart_policy"):
            if p["restart_policy"] not in v.RESTART_POLICIES:
                raise v.DockerValidationError(
                    "restart_policy is not one of: " + ", ".join(v.RESTART_POLICIES)
                )
            kwargs["restart_policy"] = {"Name": p["restart_policy"]}
        if p.get("memory"):
            kwargs["mem_limit"] = v.memory_bytes(p["memory"])
        if p.get("cpus") is not None:
            kwargs["nano_cpus"] = v.cpu_nanos(p["cpus"])
        if p.get("privileged"):
            kwargs["privileged"] = True
        if p.get("host_network"):
            kwargs["network_mode"] = "host"
            kwargs.pop("network", None)
            kwargs.pop("ports", None)  # published ports mean nothing on the host network
        if p.get("devices"):
            kwargs["devices"] = v.device_specs(p["devices"])
        if p.get("cap_add"):
            kwargs["cap_add"] = v.capabilities(p["cap_add"])
        kwargs = {k: val for k, val in kwargs.items() if val is not None}

        def work(c: Any) -> tuple[Facts, dict[str, Any]]:
            if name is not None and self._facts(c, "container", name) is not None:
                raise EngineError(
                    ErrorCode.CONFLICT,
                    f"A container named '{name}' already exists; it is not Velune's and was not touched.",
                    "Choose another name.",
                )
            ctr = c.containers.create(**kwargs)
            ctr.reload()
            return curate.container_facts(ctr.attrs), curate.container_view(
                ctr.attrs, DockerOwnership.VELUNE.value
            )

        try:
            facts, view = await self.engine.call(work, "creating a container")
        except EngineError as exc:
            if exc.code is ErrorCode.NOT_FOUND and "image" in exc.message.lower():
                exc.hint = f"Pull it first with image_pull image={image}."
            raise
        self.store.record_created(facts)
        result = ResourceResult.success("container_create", view)
        self._effect(result, "container", facts.id, facts.name, True)
        if any(b.exposed for b in ports):
            result.warnings.append("a port is published beyond loopback")
        return result

    async def _do_wait_ready(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.container_ref(p.get("container"))
        needle = str(p["log_contains"]) if p.get("log_contains") else None
        deadline = time.monotonic() + max(1, min(int(p.get("timeout", 30)), 120))
        steady = 0
        reason = "timed out"
        while True:
            state = await self.engine.call(
                lambda c: c.containers.get(ref).attrs.get("State") or {},
                "checking readiness",
                idempotent=True,
            )
            status, health = state.get("Status"), (state.get("Health") or {}).get("Status")
            if status in ("exited", "dead"):
                return ResourceResult.success(
                    "wait_ready",
                    {
                        "ready": False,
                        "reason": f"the container stopped (exit code {state.get('ExitCode')})",
                        "status": status,
                    },
                )
            if health:
                if health == "healthy":
                    return ResourceResult.success(
                        "wait_ready", {"ready": True, "reason": "healthy", "status": status}
                    )
                reason = f"health is '{health}'"
            elif needle is not None:
                raw = await self.engine.call(
                    lambda c: c.containers.get(ref).logs(tail=200, stdout=True, stderr=True),
                    "reading logs",
                    idempotent=True,
                )
                if needle in raw.decode("utf-8", "replace"):
                    return ResourceResult.success(
                        "wait_ready",
                        {"ready": True, "reason": "the expected output appeared", "status": status},
                    )
                reason = "the expected output has not appeared"
            elif status == "running":
                steady += 1
                if steady >= 2:
                    return ResourceResult.success(
                        "wait_ready",
                        {
                            "ready": True,
                            "reason": "running (no health check defined)",
                            "status": status,
                        },
                    )
            if time.monotonic() >= deadline:
                return ResourceResult.success(
                    "wait_ready", {"ready": False, "reason": reason, "status": status}
                )
            await asyncio.sleep(1.0)

    # -- images --

    async def _do_image_pull(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.image_ref(p.get("image"))
        repo, tag = v.split_image(ref)

        def work(c: Any) -> tuple[Facts | None, Facts]:
            before = self._facts(c, "image", ref)
            c.images.pull(repo, tag=tag) if "@" not in ref else c.images.pull(ref)
            after = self._facts(c, "image", ref)
            if after is None:
                raise EngineError(
                    ErrorCode.ADAPTER_FAILURE, "The pull finished but the image is not present."
                )
            return before, after

        before, after = await self.engine.call(work, "pulling an image")
        result = ResourceResult.success(
            "image_pull",
            {"image": ref, "id": curate.short_id(after.id), "already_present": before is not None},
        )
        if before is None:
            # Only an image that did not exist at all is the task's own. An existing
            # tag that a pull merely refreshed is never assumed to be.
            self.store.record_created(after)
            self._effect(result, "image", after.id, ref, True)
        else:
            result.warnings.append(
                "the tag already existed, so it is not treated as created by this task"
            )
        return result

    async def _do_image_tag(self, p: dict[str, Any]) -> ResourceResult:
        source = _image_or_id(p.get("image"))
        repository = str(p.get("repository", "")).strip()
        tag = str(p.get("tag", "")).strip()
        target = v.image_ref(f"{repository}:{tag}", "repository:tag")

        def work(c: Any) -> dict[str, Any]:
            src = self._facts(c, "image", source)
            if src is None:
                raise EngineError(ErrorCode.NOT_FOUND, f"No image '{source}'.")
            existing = self._facts(c, "image", target)
            if existing is not None and existing.id != src.id:
                raise EngineError(
                    ErrorCode.CONFLICT,
                    f"The tag '{target}' already points to a different image; it was not moved.",
                    "Pick an unused tag.",
                )
            c.images.get(src.id).tag(repository, tag=tag)
            return {"tagged": target, "id": curate.short_id(src.id)}

        return ResourceResult.success("image_tag", await self.engine.call(work, "tagging an image"))

    async def _do_image_remove(self, p: dict[str, Any]) -> ResourceResult:
        ref = _image_or_id(p.get("image"))

        def work(c: Any) -> Facts:
            facts = self._facts(c, "image", ref)
            if facts is None:
                raise EngineError(ErrorCode.NOT_FOUND, f"No image '{ref}'.")
            c.images.remove(image=ref, force=bool(p.get("force")))
            if self._facts(c, "image", facts.id) is None:
                self.store.release("image", facts.id)
            return facts

        facts = await self.engine.call(work, "removing an image")
        result = ResourceResult.success(
            "image_remove", {"removed": ref, "id": curate.short_id(facts.id)}
        )
        self._effect(result, "image", facts.id, ref, False)
        return result

    async def _do_build(self, p: dict[str, Any]) -> ResourceResult:
        path = str(p.get("path", "."))
        args = ["build", path]
        tag = p.get("tag")
        if tag:
            args = ["build", "-t", str(v.image_ref(tag, "tag")), path]
        before = None
        if tag and self.engine.connected:
            before = await asyncio.to_thread(self._peek, "image", str(tag))
        result = await self._run(args, timeout=600.0)
        if result.ok and tag and self.engine.connected and before is None:
            after = await asyncio.to_thread(self._peek, "image", str(tag))
            if after is not None:
                self.store.record_created(after)
                self._effect(result, "image", after.id, str(tag), True)
        if result.ok:
            result.data = curate.bounded_text(result.data or "", max_chars=4000, tail_lines=40)[
                "text"
            ]
        return result

    # -- volumes and networks --

    async def _do_volume_create(self, p: dict[str, Any]) -> ResourceResult:
        name = v.resource_name(p.get("name"), "name")

        def work(c: Any) -> Facts:
            if self._facts(c, "volume", name) is not None:
                raise EngineError(
                    ErrorCode.CONFLICT,
                    f"A volume named '{name}' already exists; it is not Velune's and was not touched.",
                )
            vol = c.volumes.create(name=name, labels=self.store.labels())
            vol.reload()
            return curate.volume_facts(vol.attrs)

        facts = await self.engine.call(work, "creating a volume")
        self.store.record_created(facts)
        result = ResourceResult.success("volume_create", {"name": facts.name})
        self._effect(result, "volume", facts.id, facts.name, True)
        return result

    async def _do_volume_remove(self, p: dict[str, Any]) -> ResourceResult:
        name = v.resource_name(p.get("name"), "name")

        def work(c: Any) -> Facts:
            facts = self._facts(c, "volume", name)
            if facts is None:
                raise EngineError(ErrorCode.NOT_FOUND, f"No volume '{name}'.")
            users = self._volume_users(c).get(name, [])
            if users:
                raise EngineError(
                    ErrorCode.CONFLICT,
                    f"The volume '{name}' is in use by: {', '.join(users[:5])}.",
                    "Remove or detach those containers first.",
                )
            c.volumes.get(name).remove()
            return facts

        facts = await self.engine.call(work, "removing a volume")
        self.store.release("volume", facts.id)
        result = ResourceResult.success("volume_remove", {"removed": name})
        self._effect(result, "volume", facts.id, name, False)
        return result

    async def _do_network_create(self, p: dict[str, Any]) -> ResourceResult:
        name = v.resource_name(p.get("name"), "name")
        if name in _BUILTIN_NETWORKS:
            raise v.DockerValidationError("that is a built-in network name")

        def work(c: Any) -> Facts:
            if self._facts(c, "network", name) is not None:
                raise EngineError(
                    ErrorCode.CONFLICT,
                    f"A network named '{name}' already exists; it is not Velune's and was not touched.",
                )
            net = c.networks.create(
                name, driver="bridge", internal=bool(p.get("internal")), labels=self.store.labels()
            )
            net.reload()
            return curate.network_facts(net.attrs)

        facts = await self.engine.call(work, "creating a network")
        self.store.record_created(facts)
        result = ResourceResult.success(
            "network_create", {"name": name, "id": curate.short_id(facts.id)}
        )
        self._effect(result, "network", facts.id, name, True)
        return result

    async def _network_member(self, action: str, p: dict[str, Any]) -> ResourceResult:
        net, ctr = v.resource_name(p.get("network"), "network"), v.container_ref(p.get("container"))

        def work(c: Any) -> dict[str, Any]:
            nf, cf = self._facts(c, "network", net), self._facts(c, "container", ctr)
            if nf is None or cf is None:
                raise EngineError(ErrorCode.NOT_FOUND, "The network or container does not exist.")
            network = c.networks.get(nf.id)
            (network.connect if action == "network_connect" else network.disconnect)(cf.id)
            return {"network": net, "container": ctr}

        return ResourceResult.success(action, await self.engine.call(work, "changing a network"))

    async def _do_network_connect(self, p: dict[str, Any]) -> ResourceResult:
        return await self._network_member("network_connect", p)

    async def _do_network_disconnect(self, p: dict[str, Any]) -> ResourceResult:
        return await self._network_member("network_disconnect", p)

    async def _do_network_remove(self, p: dict[str, Any]) -> ResourceResult:
        ref = v.resource_name(p.get("network"), "network")
        if ref in _BUILTIN_NETWORKS:
            raise v.DockerValidationError("built-in networks cannot be removed")

        def work(c: Any) -> Facts:
            facts = self._facts(c, "network", ref)
            if facts is None:
                raise EngineError(ErrorCode.NOT_FOUND, f"No network '{ref}'.")
            net = c.networks.get(facts.id)
            if net.attrs.get("Containers"):
                raise EngineError(
                    ErrorCode.CONFLICT,
                    f"The network '{ref}' is in use.",
                    "Detach its containers first.",
                )
            net.remove()
            return facts

        facts = await self.engine.call(work, "removing a network")
        self.store.release("network", facts.id)
        result = ResourceResult.success("network_remove", {"removed": ref})
        self._effect(result, "network", facts.id, ref, False)
        return result

    # -- compose --

    def _project_name(self, p: dict[str, Any]) -> str:
        if p.get("project"):
            return v.resource_name(str(p["project"]).lower(), "project")
        from velune.resources.docker.compose_scan import resolve_compose_file

        path = resolve_compose_file(str(p["file"]) if p.get("file") else None, self.workspace)
        base = (
            path.resolve().parent.name
            if path is not None
            else (self.workspace.name if self.workspace else "default")
        )
        cleaned = re.sub(r"[^a-z0-9_-]", "", base.lower()).lstrip("_-")
        return cleaned or "default"

    def _project_members(self, project: str) -> list[Facts] | None:
        if not self.engine.connected:
            return None
        try:
            return self.engine.call_sync(
                lambda c: [
                    curate.container_facts(x.attrs)
                    for x in c.containers.list(
                        all=True, filters={"label": f"com.docker.compose.project={project}"}
                    )
                ],
                "listing a compose project",
                idempotent=True,
            )
        except EngineError:
            return None

    def _compose_args(self, p: dict[str, Any], *tail: str) -> list[str]:
        args = ["compose", "-p", self._project_name(p)]
        if p.get("file"):
            args += ["-f", str(p["file"])]
        return [*args, *tail]

    async def _need_compose(self, action: str) -> ResourceResult | None:
        if not self._has_compose:
            return ResourceResult.failure("docker compose is not available.", action=action)
        return None

    async def _do_compose_config(self, p: dict[str, Any]) -> ResourceResult:
        from velune.resources.docker.compose_scan import compose_risks, resolve_compose_file

        path = resolve_compose_file(str(p["file"]) if p.get("file") else None, self.workspace)
        if path is None or not path.is_file():
            return ResourceResult.failure(
                "No compose file found.", action="compose_config", code=ErrorCode.NOT_FOUND
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:262144]
        except OSError:
            return ResourceResult.failure(
                "The compose file could not be read.", action="compose_config"
            )
        in_services, services = False, []
        for line in text.splitlines():
            if re.match(r"^services\s*:", line):
                in_services = True
            elif in_services and re.match(r"^\S", line) and line.strip():
                in_services = False
            elif in_services and (m := re.match(r"^  ([A-Za-z0-9][\w.-]*)\s*:", line)):
                services.append(m.group(1))
        risks = [{"scope": r.scope, "reason": r.reason} for r in compose_risks(str(path))]
        validated: bool | None = None
        if self._has_compose:
            check = await self._run(["compose", "-f", str(path), "config", "--quiet"], timeout=30.0)
            validated = check.ok
        data = {
            "file": path.name,
            "project": self._project_name({"file": str(path)}),
            "services": services[:100],
            "risks": risks,
            "needs_approval": bool(risks),
            "valid": validated,
        }
        return ResourceResult.success("compose_config", data)

    async def _do_compose_ps(self, p: dict[str, Any]) -> ResourceResult:
        project = self._project_name(p)

        def work(c: Any) -> list[dict[str, Any]]:
            return [
                {
                    **curate.container_row(x.attrs, self._owner(curate.container_facts(x.attrs))),
                    "service": (x.attrs.get("Config", {}).get("Labels") or {}).get(
                        "com.docker.compose.service"
                    ),
                }
                for x in c.containers.list(
                    all=True, filters={"label": f"com.docker.compose.project={project}"}
                )[:200]
            ]

        rows = await self.engine.call(work, "listing a compose project", idempotent=True)
        return ResourceResult.success("compose_ps", {"project": project, "containers": rows})

    async def _do_compose_logs(self, p: dict[str, Any]) -> ResourceResult:
        project = self._project_name(p)
        service = v.resource_name(p["service"], "service") if p.get("service") else None
        tail = max(1, min(int(p.get("tail") or 100), self.config.max_log_lines))

        def work(c: Any) -> list[tuple[str, bytes]]:
            label = [f"com.docker.compose.project={project}"]
            if service:
                label.append(f"com.docker.compose.service={service}")
            out = []
            for x in c.containers.list(all=True, filters={"label": label})[:20]:
                name = (x.attrs.get("Config", {}).get("Labels") or {}).get(
                    "com.docker.compose.service"
                ) or x.name
                out.append((str(name), x.logs(tail=tail, stdout=True, stderr=True)))
            return out

        pairs = await self.engine.call(work, "reading compose logs", idempotent=True)
        per = max(500, self._limit_chars() // max(1, len(pairs)))
        data = {s: curate.bounded_text(raw, max_chars=per, tail_lines=tail) for s, raw in pairs}
        return ResourceResult.success("compose_logs", {"project": project, "services": data})

    async def _do_compose_up(self, p: dict[str, Any]) -> ResourceResult:
        if (missing := await self._need_compose("compose_up")) is not None:
            return missing
        project = self._project_name(p)
        before = self._snapshot_project(project)
        result = await self._run(
            self._compose_args(p, "up", "-d"), timeout=float(self.config.compose_timeout)
        )
        if result.ok:
            self._adopt_project(result, project, before)
            result.data = curate.bounded_text(result.data or "", max_chars=4000, tail_lines=40)[
                "text"
            ]
        return result

    def _snapshot_project(self, project: str) -> dict[str, set[str]] | None:
        if not self.engine.connected:
            return None
        try:
            return self.engine.call_sync(
                lambda c: self._project_ids(c, project), "snapshotting a project", idempotent=True
            )
        except EngineError:
            return None

    @staticmethod
    def _project_ids(client: Any, project: str) -> dict[str, set[str]]:
        label = {"label": f"com.docker.compose.project={project}"}
        return {
            "container": {c.attrs["Id"] for c in client.containers.list(all=True, filters=label)},
            "network": {n.attrs["Id"] for n in client.networks.list(filters=label)},
            "volume": {x.attrs["Name"] for x in client.volumes.list(filters=label)},
        }

    def _adopt_project(
        self, result: ResourceResult, project: str, before: dict[str, set[str]] | None
    ) -> None:
        """Record what ``compose up`` *created* (new ids only) — never what it merely joined."""
        if before is None or not self.engine.connected:
            return
        try:

            def work(c: Any) -> list[Facts]:
                label = {"label": f"com.docker.compose.project={project}"}
                found: list[Facts] = []
                found += [
                    curate.container_facts(x.attrs)
                    for x in c.containers.list(all=True, filters=label)
                ]
                found += [curate.network_facts(x.attrs) for x in c.networks.list(filters=label)]
                found += [curate.volume_facts(x.attrs) for x in c.volumes.list(filters=label)]
                return found

            for facts in self.engine.call_sync(work, "reading a compose project", idempotent=True):
                if facts.id in before.get(facts.kind, set()):
                    continue  # it was already there: not created by this call
                self.store.record_created(facts, project=project)
                self._effect(result, facts.kind, facts.id, facts.name, True)
        except EngineError:
            logger.debug("could not record compose ownership", exc_info=True)

    async def _do_compose_stop(self, p: dict[str, Any]) -> ResourceResult:
        if (missing := await self._need_compose("compose_stop")) is not None:
            return missing
        return await self._run(
            self._compose_args(p, "stop"), timeout=float(self.config.compose_timeout)
        )

    async def _do_compose_down(self, p: dict[str, Any]) -> ResourceResult:
        if (missing := await self._need_compose("compose_down")) is not None:
            return missing
        project = self._project_name(p)
        members = await asyncio.to_thread(self._project_members, project)
        if members is not None:
            foreign = [
                m.name
                for m in members
                if self.store.classify(m) not in (DockerOwnership.VELUNE, DockerOwnership.PREVIOUS)
            ]
            if foreign:
                # Cleanup removes only what Velune is proven to have created. Existing
                # containers can still be removed one by one, each with its own approval.
                return ResourceResult.failure(
                    f"Compose project '{project}' includes containers Velune did not create "
                    f"({', '.join(foreign[:5])}); it will not take the project down.",
                    action="compose_down",
                    code=ErrorCode.AUTHORIZATION_DENIED,
                    hint="Remove individual containers with container_remove (each needs approval).",
                )
        args = self._compose_args(p, "down") + (["-v"] if p.get("volumes") else [])
        result = await self._run(args, timeout=300.0)
        if result.ok and self.engine.connected:
            for m in members or []:
                self.store.release("container", m.id)
                self._effect(result, "container", m.id, m.name, False)
            result.data = curate.bounded_text(result.data or "", max_chars=2000, tail_lines=20)[
                "text"
            ]
        return result


def _image_or_id(value: object) -> str:
    text = str(value or "").strip()
    if "/" in text or ":" in text or "@" in text:
        return v.image_ref(text, "image")
    if v.looks_like_id(text.removeprefix("sha256:")):
        return text
    return v.image_ref(text, "image")


def _new_session_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]
