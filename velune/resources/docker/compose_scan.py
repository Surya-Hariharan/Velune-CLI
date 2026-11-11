"""Conservative risk scan of a Docker Compose file.

``docker compose up`` creates whatever containers the file describes, so the
file — not the ``compose_up`` call — decides how dangerous the operation is. A
compose file in the workspace that sets ``privileged: true``, joins the host
network, bind-mounts the host's filesystem or hands a device to a container must
not run unattended in AUTO mode (VAIR Decision E): those need explicit approval.

This is a *classifier*, not a YAML parser (PyYAML is not a dependency). It reads
the file line by line and errs toward flagging: a false positive only means the
user is asked; a false negative would let a risky container start unprompted.
Anything it cannot inspect — an unreadable or oversized file, or ``include:`` /
``extends:`` pulling in files it did not read — is reported as unverified, which
the caller treats as risky too.

Pure and side-effect free apart from reading the one file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Files larger than this are not scanned (reported as unverified).
MAX_COMPOSE_BYTES = 256 * 1024

#: What ``docker compose`` looks for when no ``-f`` is given.
COMPOSE_FILE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

# Scopes (see velune.permissions.actions): precise, so an audit log shows *why*.
SCOPE_PRIVILEGED = "docker.container.privileged"
SCOPE_HOST_NETWORK = "docker.network.host"
SCOPE_HOST_MOUNT = "docker.mount.host"
SCOPE_DEVICE = "docker.device.access"
SCOPE_UNVERIFIED = "docker.compose.unverified"
SCOPE_EXTERNAL = "docker.compose.external"


@dataclass(frozen=True, slots=True)
class ComposeRisk:
    """One reason a compose file needs explicit approval."""

    scope: str
    reason: str


_FALSY = frozenset({"false", "no", "off", "0", "[]", "{}", ""})
_HOST_PATH = re.compile(r"^(?:/|~|\.\.[\\/]|[A-Za-z]:[\\/])")
# ``key: value`` (optionally a list item ``- key: value``).
_KV = re.compile(r"^\s*(?:-\s+)?([A-Za-z_][\w.-]*)\s*:\s*(.*?)\s*$")
_LIST_ITEM = re.compile(r"^\s*-\s+(.*?)\s*$")
_COMMENT = re.compile(r"\s+#.*$")

_NAMESPACE_KEYS = {
    "network_mode": (SCOPE_HOST_NETWORK, "joins the host's network stack"),
    "pid": (SCOPE_PRIVILEGED, "shares the host's process namespace"),
    "ipc": (SCOPE_PRIVILEGED, "shares the host's IPC namespace"),
    "uts": (SCOPE_PRIVILEGED, "shares the host's UTS namespace"),
    "userns_mode": (SCOPE_PRIVILEGED, "shares the host's user namespace"),
    "cgroup": (SCOPE_PRIVILEGED, "shares the host's cgroup namespace"),
}
_PRESENCE_KEYS = {
    "cap_add": (SCOPE_PRIVILEGED, "adds Linux capabilities"),
    "security_opt": (SCOPE_PRIVILEGED, "changes the container's security options"),
    "devices": (SCOPE_DEVICE, "hands host devices to a container"),
    "device_cgroup_rules": (SCOPE_DEVICE, "grants device access through cgroup rules"),
}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _is_host_path(value: str) -> bool:
    return bool(_HOST_PATH.match(_unquote(value)))


def _bind_item(item: str) -> bool:
    """A short-syntax volume entry (``/host:/container``) whose source is a host path."""
    text = _unquote(item)
    if not _HOST_PATH.match(text):
        return False
    rest = text[2:] if re.match(r"^[A-Za-z]:[\\/]", text) else text
    return ":" in rest


def scan_compose_text(text: str) -> list[ComposeRisk]:
    """Return the risks found in compose-file *text* (empty when none)."""
    found: dict[tuple[str, str], ComposeRisk] = {}

    def add(scope: str, reason: str) -> None:
        found.setdefault((scope, reason), ComposeRisk(scope, reason))

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = _COMMENT.sub("", raw)
        lowered = line.lower()

        if "docker.sock" in lowered:
            add(SCOPE_HOST_MOUNT, "mounts the Docker socket (root-equivalent on the host)")

        item = _LIST_ITEM.match(line)
        if item and _bind_item(item.group(1)):
            add(SCOPE_HOST_MOUNT, "bind-mounts a host path outside the project")

        kv = _KV.match(line)
        if kv is None:
            continue
        key, value = kv.group(1).lower(), kv.group(2)
        plain = _unquote(value).lower()

        if key == "privileged" and plain not in _FALSY:
            add(SCOPE_PRIVILEGED, "runs a privileged container")
        elif key == "network_mode" and re.match(r"^container:", plain):
            add(SCOPE_PRIVILEGED, "joins an existing container's network namespace")
        elif key in _NAMESPACE_KEYS:
            scope, reason = _NAMESPACE_KEYS[key]
            if re.search(r"\bhost\b", plain) or "${" in plain:
                add(scope, reason)
        elif key in _PRESENCE_KEYS and plain not in {"[]", "{}"}:
            scope, reason = _PRESENCE_KEYS[key]
            add(scope, reason)
        elif key in ("source", "device") and value and _is_host_path(value):
            add(SCOPE_HOST_MOUNT, "mounts a host path outside the project")
        elif key == "external" and plain not in {"false", "no", "off", "0"}:
            add(
                SCOPE_EXTERNAL,
                "uses an external volume or network that already exists (the project does not own it)",
            )
        elif key == "volumes_from":
            add(SCOPE_EXTERNAL, "mounts the volumes of another container")
        elif key in ("include", "extends"):
            add(SCOPE_UNVERIFIED, f"uses '{key}', which pulls in files that were not inspected")

    return list(found.values())


def resolve_compose_file(file: str | None, base: Path | None = None) -> Path | None:
    """The compose file ``docker compose`` would use, or None when there is none."""
    root = Path(base) if base is not None else Path.cwd()
    if file:
        path = Path(file)
        return path if path.is_absolute() else root / path
    for name in COMPOSE_FILE_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def compose_risks(file: str | None, base: Path | None = None) -> list[ComposeRisk]:
    """Risks of running ``docker compose -f <file> up`` (default file when None).

    No compose file at all is not a risk (Docker will simply fail). A file that
    exists but cannot be inspected is reported as unverified.
    """
    path = resolve_compose_file(file, base)
    if path is None:
        return []
    try:
        if not path.exists():
            return []  # docker reports the missing file itself
        if not path.is_file():
            return [ComposeRisk(SCOPE_UNVERIFIED, "the compose path is not a regular file")]
        with path.open("rb") as handle:
            data = handle.read(MAX_COMPOSE_BYTES + 1)
    except OSError:
        return [ComposeRisk(SCOPE_UNVERIFIED, "the compose file could not be read")]
    if len(data) > MAX_COMPOSE_BYTES:
        return [ComposeRisk(SCOPE_UNVERIFIED, "the compose file is too large to inspect")]
    return scan_compose_text(data.decode("utf-8", errors="replace"))
