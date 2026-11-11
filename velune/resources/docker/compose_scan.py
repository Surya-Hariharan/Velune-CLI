"""Conservative risk scan of a Docker Compose file.

``docker compose up`` creates whatever containers the file describes, so the
file — not the ``compose_up`` call — decides how dangerous the operation is. A
compose file in the workspace that sets ``privileged: true``, joins the host
network, bind-mounts the host's filesystem or hands a device to a container must
not run unattended in AUTO mode (VAIR Decision E): those need explicit approval.

Two passes, and the result is their union (so the second can only add findings):

1. a line classifier, which needs nothing but the text and errs toward flagging;
2. a **structural** pass (VAIR A5) over the parsed document, which sees what a line
   scan cannot: flow-style YAML (``{privileged: true}``, ``volumes: ["/:/host"]``),
   quoted keys, anchors and merge keys, JSON-formatted files, and bind sources that
   are variables. It uses PyYAML's ``safe_load`` (never ``load``) with size, depth and
   node limits. PyYAML is an optional dependency (the ``docker`` extra): when it is
   missing, a file using any construct the line classifier cannot read is reported as
   *unverified* instead of being waved through.

A false positive only means the user is asked; a false negative would let a risky
container start unprompted. Anything it cannot inspect (an unreadable or oversized
file, a file that does not parse, or ``include:`` / ``extends:`` pulling in files it
did not read) is reported as unverified, which the caller treats as risky too.

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


def _flow_items(line: str) -> list[str]:
    """Elements of a flow-style list on one line (``volumes: ["/a:/b", "c:/d"]``)."""
    match = re.search(r":\s*\[(.*)\]\s*$", line)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _bind_item(item: str) -> bool:
    """A short-syntax volume entry (``/host:/container``) whose source is a host path."""
    text = _unquote(item)
    if not _HOST_PATH.match(text):
        return False
    rest = text[2:] if re.match(r"^[A-Za-z]:[\\/]", text) else text
    return ":" in rest


_MAX_DEPTH = 32
_MAX_NODES = 50_000
_MAX_DOCUMENTS = 8
#: Syntax the line classifier cannot read; without a YAML parser it makes a file unverified.
_OPAQUE_SYNTAX = (
    re.compile(r"(?<!\$)\{"),  # a flow mapping (``${VAR}`` is interpolation, not one)
    re.compile(r"^\s*(?:-\s+)?[\"'][^\"']+[\"']\s*:", re.MULTILINE),  # a quoted key
    re.compile(r"(?:^|[\s:\[,-])[&*][A-Za-z_][\w-]*"),  # an anchor or an alias
    re.compile(r"<<\s*:"),  # a merge key
    re.compile(r"(?:^|[\s:\[,-])!{1,2}[A-Za-z]"),  # a tag
    re.compile(r"^\s*---\s*\S", re.MULTILINE),  # content on the document marker line
)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().strip("\"'").lower() not in _FALSY


def _is_variable(text: str) -> bool:
    return text.lstrip().startswith("$")


def _bind_source(entry: object) -> str | None:
    """The host-side source of one ``volumes`` entry, or None when it is not a bind."""
    if isinstance(entry, str):
        text = _unquote(entry)
        if _is_variable(text):
            return text
        if not _HOST_PATH.match(text):
            return None
        rest = text[2:] if re.match(r"^[A-Za-z]:[\\/]", text) else text
        return text if ":" in rest else None
    if isinstance(entry, dict):
        kind = str(entry.get("type") or "").lower()
        source = entry.get("source")
        if kind in ("volume", "tmpfs", "npipe", "cluster") or not isinstance(source, str):
            return None
        if _is_variable(source) or _HOST_PATH.match(_unquote(source)):
            return source
    return None


def _scan_service(name: str, svc: dict, add) -> None:
    if _truthy(svc.get("privileged")):
        add(SCOPE_PRIVILEGED, "runs a privileged container")
    mode = str(svc.get("network_mode") or "").strip().lower()
    if mode == "host" or "${" in mode:
        add(SCOPE_HOST_NETWORK, "joins the host's network stack")
    elif mode.startswith("container:"):
        add(SCOPE_PRIVILEGED, "joins an existing container's network namespace")
    for key, (scope, reason) in _NAMESPACE_KEYS.items():
        if key == "network_mode":
            continue
        value = str(svc.get(key) or "").strip().lower()
        if value == "host" or "${" in value:
            add(scope, reason)
    for key, (scope, reason) in _PRESENCE_KEYS.items():
        value = svc.get(key)
        if value not in (None, "", [], {}):
            add(scope, reason)
    if svc.get("gpus") not in (None, "", [], {}):
        add(SCOPE_DEVICE, "hands host GPUs to a container")
    volumes = svc.get("volumes")
    for entry in volumes if isinstance(volumes, list) else []:
        text = entry if isinstance(entry, str) else str(entry)
        if "docker.sock" in text.lower():
            add(SCOPE_HOST_MOUNT, "mounts the Docker socket (root-equivalent on the host)")
        if _bind_source(entry) is not None:
            add(SCOPE_HOST_MOUNT, "bind-mounts a host path outside the project")
    if svc.get("volumes_from") not in (None, "", []):
        add(SCOPE_EXTERNAL, "mounts the volumes of another container")
    if "extends" in svc:
        add(SCOPE_UNVERIFIED, "uses 'extends', which pulls in files that were not inspected")
    build = svc.get("build")
    context = build.get("context") if isinstance(build, dict) else build
    if isinstance(context, str) and (_is_variable(context) or _HOST_PATH.match(_unquote(context))):
        add(SCOPE_HOST_MOUNT, "builds from a host path outside the project")


def _scan_top_level(doc: dict, add) -> None:
    for section, entry_scope in (("volumes", True), ("networks", False)):
        group = doc.get(section)
        for entry in group.values() if isinstance(group, dict) else []:
            if not isinstance(entry, dict):
                continue
            if _truthy(entry.get("external")):
                add(
                    SCOPE_EXTERNAL,
                    "uses an external volume or network that already exists "
                    "(the project does not own it)",
                )
            opts = entry.get("driver_opts")
            device = opts.get("device") if isinstance(opts, dict) else None
            if (
                entry_scope
                and isinstance(device, str)
                and (_is_variable(device) or _HOST_PATH.match(_unquote(device)))
            ):
                add(SCOPE_HOST_MOUNT, "mounts a host path outside the project")
    for section in ("configs", "secrets"):
        group = doc.get(section)
        for entry in group.values() if isinstance(group, dict) else []:
            path = entry.get("file") if isinstance(entry, dict) else None
            if isinstance(path, str) and (_is_variable(path) or _HOST_PATH.match(_unquote(path))):
                add(SCOPE_HOST_MOUNT, "reads a host file outside the project")
    if "include" in doc:
        add(SCOPE_UNVERIFIED, "uses 'include', which pulls in files that were not inspected")


def _structure_within_limits(node: object) -> bool:
    """False for a document too deep or too large to walk safely (anchor bombs)."""
    stack: list[tuple[object, int]] = [(node, 0)]
    seen = 0
    visited: set[int] = set()
    while stack:
        item, depth = stack.pop()
        seen += 1
        if depth > _MAX_DEPTH or seen > _MAX_NODES:
            return False
        if isinstance(item, (dict, list)):
            if id(item) in visited:  # an alias to something already counted
                continue
            visited.add(id(item))
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def _scan_structure(text: str, add) -> bool:
    """Structural pass. True when it ran to completion (so the file is verified)."""
    try:
        import yaml
    except ImportError:
        return False
    try:
        documents = []
        for index, doc in enumerate(yaml.safe_load_all(text)):
            if index >= _MAX_DOCUMENTS:
                add(SCOPE_UNVERIFIED, "the compose file has too many YAML documents to inspect")
                return False
            documents.append(doc)
    except (yaml.YAMLError, RecursionError, ValueError):
        add(SCOPE_UNVERIFIED, "the compose file could not be parsed as YAML")
        return False
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        if not _structure_within_limits(doc):
            add(SCOPE_UNVERIFIED, "the compose file is too deeply nested to inspect")
            return False
        _scan_top_level(doc, add)
        services = doc.get("services")
        for name, svc in services.items() if isinstance(services, dict) else []:
            if isinstance(svc, dict):
                _scan_service(str(name), svc, add)
    return True


def scan_compose_text(text: str) -> list[ComposeRisk]:
    """Return the risks found in compose-file *text* (empty when none)."""
    found: dict[tuple[str, str], ComposeRisk] = {}

    def add(scope: str, reason: str) -> None:
        found.setdefault((scope, reason), ComposeRisk(scope, reason))

    if not _scan_structure(text, add) and any(p.search(text) for p in _OPAQUE_SYNTAX):
        add(
            SCOPE_UNVERIFIED,
            "the file uses YAML the built-in scan cannot read (flow mappings, quoted keys, "
            "anchors, merge keys or tags); install PyYAML (the docker extra) to inspect it",
        )

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
        for element in _flow_items(line):
            if _bind_item(element) or _is_variable(_unquote(element)) and ":" in element:
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
