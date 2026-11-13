"""An in-memory Docker daemon that speaks the slice of the SDK the connector uses (VAIR A4).

Deterministic and offline: the connector is given a :class:`FakeDaemon` through a
client factory, so every code path — create, ownership, collisions, removal,
daemon restarts — runs without Docker. Objects are plain attribute dicts in the shape
of the real Engine API, which is what the connector's curation reads.
"""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from velune.resources.docker.engine import DockerEngine, Endpoint

_ids = itertools.count(1)


class NotFound(Exception):
    """Name-compatible with ``docker.errors.NotFound`` (the connector matches by class name)."""


class APIError(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.explanation = message


class ConnectionError(OSError):  # noqa: A001 - mirrors requests.exceptions.ConnectionError by name
    pass


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


class FakeObject:
    def __init__(self, daemon: FakeDaemon, attrs: dict[str, Any]) -> None:
        self._daemon = daemon
        self.attrs = attrs

    def reload(self) -> None:
        self._daemon.check()


class FakeContainer(FakeObject):
    @property
    def name(self) -> str:
        return self.attrs["Name"].lstrip("/")

    @property
    def id(self) -> str:
        return self.attrs["Id"]

    def _state(self, status: str, **extra: Any) -> None:
        self._daemon.check()
        self._daemon.calls.append((status, self.id))
        self.attrs["State"].update({"Status": status, "Running": status == "running", **extra})

    def start(self) -> None:
        self._state("running")

    def stop(self, timeout: int = 10) -> None:
        self._state("exited", ExitCode=0)

    def restart(self, timeout: int = 10) -> None:
        self._state("running")

    def pause(self) -> None:
        self._state("paused")

    def unpause(self) -> None:
        self._state("running")

    def remove(self, force: bool = False, v: bool = False) -> None:
        self._daemon.check()
        if self.attrs["State"]["Running"] and not force:
            raise APIError("cannot remove a running container", 409)
        self._daemon.calls.append(("remove", self.id))
        self._daemon.containers.pop(self.id)

    def logs(self, **kwargs: Any) -> bytes:
        self._daemon.check()
        return self.attrs.get("_logs", b"")

    def stats(self, stream: bool = False) -> dict[str, Any]:
        self._daemon.check()
        return {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200},
                "system_cpu_usage": 2000,
                "online_cpus": 2,
            },
            "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
            "memory_stats": {"usage": 50_000_000, "limit": 1_000_000_000},
            "networks": {"eth0": {"rx_bytes": 10, "tx_bytes": 20}},
            "pids_stats": {"current": 3},
        }


class FakeImage(FakeObject):
    def tag(self, repository: str, tag: str | None = None) -> bool:
        self._daemon.check()
        self.attrs["RepoTags"].append(f"{repository}:{tag or 'latest'}")
        return True


class FakeVolume(FakeObject):
    def remove(self, force: bool = False) -> None:
        self._daemon.check()
        self._daemon.volumes.pop(self.attrs["Name"])


class FakeNetwork(FakeObject):
    def connect(self, container: str, **kwargs: Any) -> None:
        self._daemon.check()
        ctr = self._daemon.containers[container]
        self.attrs["Containers"][container] = {"Name": ctr["Name"].lstrip("/")}

    def disconnect(self, container: str, **kwargs: Any) -> None:
        self._daemon.check()
        self.attrs["Containers"].pop(container, None)

    def remove(self) -> None:
        self._daemon.check()
        self._daemon.networks.pop(self.attrs["Id"])


class Collection:
    def __init__(
        self, daemon: FakeDaemon, store: dict[str, dict[str, Any]], cls: type[FakeObject]
    ) -> None:
        self._daemon, self._store, self._cls = daemon, store, cls

    def _find(self, ref: str) -> dict[str, Any] | None:
        for attrs in self._store.values():
            names = {
                attrs.get("Id"),
                attrs.get("Name", "").lstrip("/"),
                *(attrs.get("RepoTags") or []),
            }
            if ref in names or (
                attrs.get("Id") and str(attrs["Id"]).startswith(ref) and len(ref) >= 12
            ):
                return attrs
        return None

    def get(self, ref: str) -> Any:
        self._daemon.check()
        attrs = self._find(ref)
        if attrs is None:
            raise NotFound(f"No such object: {ref}")
        return self._cls(self._daemon, attrs)

    def list(self, all: bool = False, filters: dict[str, Any] | None = None, **_: Any) -> list[Any]:
        self._daemon.check()
        out = []
        wanted = (filters or {}).get("label")
        wanted = [wanted] if isinstance(wanted, str) else list(wanted or [])
        for attrs in self._store.values():
            labels = (attrs.get("Config") or {}).get("Labels") or attrs.get("Labels") or {}
            if any(
                f"{k}={v}" not in [f"{a}={b}" for a, b in labels.items()]
                for k, v in (w.split("=", 1) for w in wanted)
            ):
                continue
            if self._cls is FakeContainer and not all and not attrs["State"]["Running"]:
                continue
            out.append(self._cls(self._daemon, attrs))
        return out


class Containers(Collection):
    def create(self, **kw: Any) -> FakeContainer:
        self._daemon.check()
        self._daemon.created_kwargs.append(kw)
        image = kw["image"]
        if self._daemon.images_collection._find(image) is None:
            raise NotFound(f"No such image: {image}")
        name = kw.get("name") or f"auto-{next(_ids)}"
        if self._find(name) is not None:
            raise APIError("name already in use", 409)
        cid = _hex(f"ctr{next(_ids)}")
        binds = kw.get("volumes") or {}
        attrs = {
            "Id": cid,
            "Name": f"/{name}",
            "Created": f"2026-01-01T00:00:{next(_ids):02d}Z",
            "State": {"Status": "created", "Running": False, "ExitCode": 0},
            "Config": {
                "Image": image,
                "Cmd": kw.get("command"),
                "Env": [f"{k}={v}" for k, v in (kw.get("environment") or {}).items()],
                "Labels": dict(kw.get("labels") or {}),
            },
            "HostConfig": {
                "Privileged": bool(kw.get("privileged")),
                "NetworkMode": kw.get("network_mode") or "default",
                "RestartPolicy": kw.get("restart_policy") or {},
                "Memory": kw.get("mem_limit", 0),
                "CapAdd": kw.get("cap_add"),
            },
            "NetworkSettings": {"Ports": {}, "Networks": {}},
            "Mounts": [
                {
                    "Type": "bind" if str(src).startswith(("/", "C:", "c:")) else "volume",
                    "Source": src,
                    "Name": src,
                    "Destination": v["bind"],
                    "RW": v["mode"] == "rw",
                }
                for src, v in binds.items()
            ],
        }
        self._store[cid] = attrs
        return FakeContainer(self._daemon, attrs)


class Images(Collection):
    def pull(self, repository: str, tag: str | None = None, **_: Any) -> FakeImage:
        self._daemon.check()
        self._daemon.calls.append(("pull", f"{repository}:{tag}"))
        ref = f"{repository}:{tag or 'latest'}"
        existing = self._find(ref)
        if existing is None:
            self._daemon.add_image(ref)
        else:
            existing["Id"] = existing["Id"]  # unchanged: refreshed in place
        return self.get(ref)

    def remove(self, image: str, force: bool = False, **_: Any) -> None:
        self._daemon.check()
        attrs = self._find(image)
        if attrs is None:
            raise NotFound(image)
        tags = attrs.get("RepoTags") or []
        if image in tags and len(tags) > 1:
            tags.remove(image)
            return
        self._store.pop(attrs["Id"])


class Volumes(Collection):
    def create(self, name: str, labels: dict[str, str] | None = None, **_: Any) -> FakeVolume:
        self._daemon.check()
        attrs = {
            "Name": name,
            "Driver": "local",
            "CreatedAt": f"2026-01-01T00:01:{next(_ids):02d}Z",
            "Labels": labels or {},
        }
        self._store[name] = attrs
        return FakeVolume(self._daemon, attrs)


class Networks(Collection):
    def create(
        self,
        name: str,
        driver: str = "bridge",
        internal: bool = False,
        labels: dict[str, str] | None = None,
        **_: Any,
    ) -> FakeNetwork:
        self._daemon.check()
        nid = _hex(f"net{next(_ids)}")
        attrs = {
            "Id": nid,
            "Name": name,
            "Driver": driver,
            "Scope": "local",
            "Internal": internal,
            "Created": f"2026-01-01T00:02:{next(_ids):02d}Z",
            "Labels": labels or {},
            "Containers": {},
        }
        self._store[nid] = attrs
        return FakeNetwork(self._daemon, attrs)


class FakeDaemon:
    """The daemon's state plus failure switches (``down``, ``api_version``)."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self._images: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.networks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.created_kwargs: list[dict[str, Any]] = []
        self.down = False
        self.api_version = "1.45"
        self.daemon_id = "DAEMON-1"
        self.clients_opened = 0
        self.clients_closed = 0
        self.images_collection = Images(self, self._images, FakeImage)
        for name in ("bridge", "host", "none"):
            nid = _hex(f"builtin{name}")
            self.networks[nid] = {
                "Id": nid,
                "Name": name,
                "Driver": name,
                "Scope": "local",
                "Internal": False,
                "Created": "2020-01-01T00:00:00Z",
                "Labels": {},
                "Containers": {},
            }

    def check(self) -> None:
        if self.down:
            raise ConnectionError("daemon unreachable")

    def add_image(self, ref: str, labels: dict[str, str] | None = None) -> dict[str, Any]:
        iid = "sha256:" + _hex(f"img{ref}{next(_ids)}")
        attrs = {
            "Id": iid,
            "RepoTags": [ref],
            "Size": 5_000_000,
            "Created": "2025-12-01T00:00:00Z",
            "Config": {"Labels": labels or {}, "Env": ["PATH=/bin", "SECRET=hunter2hunter2"]},
        }
        self._images[iid] = attrs
        return attrs

    def add_container(
        self,
        name: str,
        *,
        running: bool = True,
        labels: dict[str, str] | None = None,
        image: str = "nginx:latest",
        env: list[str] | None = None,
        logs: bytes = b"",
    ) -> dict[str, Any]:
        cid = _hex(f"pre{name}{next(_ids)}")
        attrs = {
            "Id": cid,
            "Name": f"/{name}",
            "Created": f"2025-01-01T00:00:{next(_ids):02d}Z",
            "State": {
                "Status": "running" if running else "exited",
                "Running": running,
                "ExitCode": 0,
            },
            "Config": {"Image": image, "Cmd": ["run"], "Env": env or [], "Labels": labels or {}},
            "HostConfig": {},
            "NetworkSettings": {"Ports": {}, "Networks": {}},
            "Mounts": [],
            "_logs": logs,
        }
        self.containers[cid] = attrs
        return attrs

    def add_volume(self, name: str, labels: dict[str, str] | None = None) -> dict[str, Any]:
        attrs = {
            "Name": name,
            "Driver": "local",
            "CreatedAt": f"2025-01-01T00:03:{next(_ids):02d}Z",
            "Labels": labels or {},
        }
        self.volumes[name] = attrs
        return attrs

    def add_network(self, name: str) -> dict[str, Any]:
        nid = _hex(f"preNet{name}")
        attrs = {
            "Id": nid,
            "Name": name,
            "Driver": "bridge",
            "Scope": "local",
            "Internal": False,
            "Created": "2025-01-01T00:04:00Z",
            "Labels": {},
            "Containers": {},
        }
        self.networks[nid] = attrs
        return attrs

    def client(self) -> SimpleNamespace:
        self.clients_opened += 1
        daemon = self
        client = SimpleNamespace()
        client.containers = Containers(daemon, daemon.containers, FakeContainer)
        client.images = daemon.images_collection
        client.volumes = Volumes(daemon, daemon.volumes, FakeVolume)
        client.networks = Networks(daemon, daemon.networks, FakeNetwork)
        client.ping = lambda: (daemon.check(), True)[1]
        client.version = lambda: (
            daemon.check(),
            {"Version": "27.0.1", "ApiVersion": daemon.api_version},
        )[1]
        client.info = lambda: (
            daemon.check(),
            {
                "ID": daemon.daemon_id,
                "NCPU": 4,
                "MemTotal": 8_000_000_000,
                "Containers": len(daemon.containers),
                "OperatingSystem": "Fake OS",
                "Architecture": "x86_64",
            },
        )[1]
        client.close = lambda: setattr(daemon, "clients_closed", daemon.clients_closed + 1)
        return client


def make_engine(daemon: FakeDaemon, **kw: Any) -> DockerEngine:
    def factory(endpoint: Endpoint, timeout: float) -> Any:
        return daemon.client()

    kw.setdefault("environ", {})
    kw.setdefault("platform", "linux")
    kw.setdefault("home", Path("/home/user"))
    kw.setdefault("runtime_dir", "")
    return DockerEngine(client_factory=factory, **kw)
