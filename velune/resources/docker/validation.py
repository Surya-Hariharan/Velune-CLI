"""Validation of every model-supplied Docker value (VAIR A4).

Nothing the model sends reaches the Docker SDK (or a ``docker`` argv) unless it
passed one of these validators. They are deliberately strict allowlists:

* identifiers (container, image, volume, network) cannot start with ``-`` or
  contain path separators, whitespace or shell metacharacters, so they can never
  be read as an option and never name a path;
* a *container reference* is a name or an id — ownership is decided on the
  resolved **id**, never on the text;
* ports bind to loopback unless an address is given explicitly (which the
  connector then treats as exposure);
* a mount source is classified (named volume / host path / Docker socket) so the
  caller can escalate it;
* labels in the ``dev.velune.`` and ``com.docker.compose.`` namespaces are
  reserved: a model cannot forge an ownership claim.

Every function raises :class:`DockerValidationError`; messages name the rule, not
the offending value.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_ENV = 64
MAX_PORTS = 32
MAX_MOUNTS = 16
MAX_LABELS = 32
MAX_COMMAND_ARGS = 64
MAX_VALUE = 4096

_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_LABEL_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,126}$")
_HEX_ID = re.compile(r"^[0-9a-f]{12,64}$")
_PORT = re.compile(
    r"^(?:(?P<ip>[0-9a-fA-F:.\[\]]+):)?(?P<host>\d{1,5}):(?P<container>\d{1,5})(?:/(?P<proto>tcp|udp))?$"
)
_MEMORY = re.compile(r"^(?P<n>[1-9]\d{0,9})(?P<unit>[kKmMgG]?)$")
# repository[:tag][@digest]; each path component lowercase alnum with separators.
_IMAGE = re.compile(
    r"^(?:(?P<host>[a-zA-Z0-9.-]+(?::\d{1,5})?)/)?"
    r"(?P<path>[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)
RESTART_POLICIES = ("no", "on-failure", "unless-stopped", "always")
RESERVED_LABEL_PREFIXES = ("dev.velune.", "com.docker.compose.", "com.docker.stack.")
_SOCKET_NAMES = ("docker.sock", "docker_engine", "podman.sock", "containerd.sock")


class DockerValidationError(ValueError):
    """A rejected value (the message is safe to show the model)."""


def _text(value: object, what: str, limit: int = MAX_VALUE) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DockerValidationError(f"{what} must be non-empty text")
    if len(value) > limit or "\x00" in value:
        raise DockerValidationError(f"{what} is too long or contains a NUL character")
    return value.strip()


def container_ref(value: object, what: str = "container") -> str:
    """A container name or id."""
    text = _text(value, what, 128)
    if not _NAME.match(text):
        raise DockerValidationError(
            f"{what} must be a name or id of letters, digits, '_', '.' or '-' "
            "(it may not start with '-' or contain a path or spaces)"
        )
    return text


def resource_name(value: object, what: str) -> str:
    """A volume or network name (or id)."""
    text = _text(value, what, 128)
    if not _NAME.match(text):
        raise DockerValidationError(
            f"{what} must be letters, digits, '_', '.' or '-' and not start with '-'"
        )
    return text


def looks_like_id(text: str) -> bool:
    return bool(_HEX_ID.match(text))


def image_ref(value: object, what: str = "image") -> str:
    """A syntactically valid image reference (``registry/repo:tag@sha256:…``)."""
    text = _text(value, what, 255)
    if text.startswith(("-", ".", "/")) or not _IMAGE.match(text):
        raise DockerValidationError(
            f"{what} is not a valid image reference (e.g. 'nginx:1.27' or 'ghcr.io/org/app:v1')"
        )
    return text


def split_image(ref: str) -> tuple[str, str]:
    """``(repository, tag)`` for a validated reference (tag defaults to ``latest``)."""
    match = _IMAGE.match(ref)
    if not match:
        raise DockerValidationError("not a valid image reference")
    host = f"{match.group('host')}/" if match.group("host") else ""
    return host + match.group("path"), match.group("tag") or "latest"


def registry_of(ref: str) -> str:
    """The registry host an image reference pulls from (``docker.io`` when implicit)."""
    match = _IMAGE.match(ref)
    host = match.group("host") if match else None
    if host and ("." in host or ":" in host or host == "localhost"):
        return host
    return "docker.io"


def command_args(value: object) -> list[str]:
    """A command line split with POSIX quoting rules into argv (never run through a shell)."""
    text = _text(value, "command", 8192)
    try:
        args = shlex.split(text, posix=True)
    except ValueError:
        raise DockerValidationError("command has unbalanced quotes") from None
    if not args or len(args) > MAX_COMMAND_ARGS:
        raise DockerValidationError(f"command must have 1-{MAX_COMMAND_ARGS} arguments")
    return args


def env_pairs(values: object) -> dict[str, str]:
    if values in (None, []):
        return {}
    if not isinstance(values, list) or len(values) > MAX_ENV:
        raise DockerValidationError(f"env must be a list of at most {MAX_ENV} NAME=value items")
    env: dict[str, str] = {}
    for item in values:
        name, sep, val = str(item).partition("=")
        if not sep or not _ENV_NAME.match(name):
            raise DockerValidationError("each env item must look like NAME=value")
        if len(val) > MAX_VALUE or "\x00" in val:
            raise DockerValidationError(f"the value of {name} is too long")
        env[name] = val
    return env


@dataclass(frozen=True, slots=True)
class PortBinding:
    container_port: int
    protocol: str
    host_ip: str
    host_port: int

    @property
    def exposed(self) -> bool:
        """Bound beyond loopback (reachable from the network)."""
        try:
            return not ipaddress.ip_address(self.host_ip).is_loopback
        except ValueError:
            return True

    def key(self) -> str:
        return f"{self.container_port}/{self.protocol}"


def port_bindings(values: object) -> list[PortBinding]:
    """``[ip:]host:container[/proto]``; the host address defaults to 127.0.0.1."""
    if values in (None, []):
        return []
    if not isinstance(values, list) or len(values) > MAX_PORTS:
        raise DockerValidationError(f"ports must be a list of at most {MAX_PORTS} items")
    bindings: list[PortBinding] = []
    for item in values:
        match = _PORT.match(str(item).strip())
        if not match:
            raise DockerValidationError("each port must look like [ip:]host:container[/tcp|udp]")
        host, container = int(match.group("host")), int(match.group("container"))
        if not (0 < host < 65536 and 0 < container < 65536):
            raise DockerValidationError("a port is out of range")
        ip = (match.group("ip") or "127.0.0.1").strip("[]")
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise DockerValidationError("a port's host address is not a valid IP address") from None
        bindings.append(PortBinding(container, match.group("proto") or "tcp", ip, host))
    return bindings


@dataclass(frozen=True, slots=True)
class MountSpec:
    kind: str  # "volume" | "bind"
    source: str  # volume name, or a host path (resolved for binds)
    target: str
    read_only: bool
    docker_socket: bool = False
    inside_workspace: bool = False
    secret: bool = False


def _is_host_path(source: str) -> bool:
    return (
        source.startswith(("/", "~", "."))
        or bool(re.match(r"^[A-Z]:[\\/]", source))
        or source.startswith("\\\\")
    )


def mount_specs(values: object, workspace: Path | None) -> list[MountSpec]:
    """``source:target[:ro|rw]`` entries, each classified for risk."""
    if values in (None, []):
        return []
    if not isinstance(values, list) or len(values) > MAX_MOUNTS:
        raise DockerValidationError(f"volumes must be a list of at most {MAX_MOUNTS} items")
    from velune.permissions.boundary import is_secret_path

    specs: list[MountSpec] = []
    for item in values:
        raw = _text(item, "a volume entry", 1024)
        mode = "rw"
        body = raw
        for suffix in (":ro", ":rw"):
            if raw.endswith(suffix):
                body, mode = raw[: -len(suffix)], suffix[1:]
        # split source from target at the last ':' that precedes an absolute container path
        match = re.match(r"^(?P<src>.+?):(?P<dst>/[^:]*)$", body)
        if not match:
            raise DockerValidationError(
                "each volume must look like source:/container/path[:ro] (a named volume or a host path)"
            )
        source, target = match.group("src"), match.group("dst")
        normalized = posixpath.normpath(target)
        if ".." in PurePosixPath(target).parts or (
            target != "/" and normalized != target.rstrip("/")
        ):
            raise DockerValidationError("a container mount path must be absolute and normalized")
        if _is_host_path(source):
            host = Path(source).expanduser()
            if not host.is_absolute():
                host = (workspace or Path.cwd()) / host
            try:
                resolved = host.resolve()
            except OSError:
                raise DockerValidationError("a host path could not be resolved") from None
            inside = False
            if workspace is not None:
                try:
                    resolved.relative_to(workspace.resolve())
                    inside = True
                except ValueError:
                    inside = False
            low = str(resolved).lower().replace("\\", "/")
            sock = any(low.endswith(n) or f"/{n}" in low for n in _SOCKET_NAMES)
            specs.append(
                MountSpec(
                    "bind",
                    str(resolved),
                    target,
                    mode == "ro",
                    sock,
                    inside,
                    is_secret_path(resolved),
                )
            )
        else:
            if not _NAME.match(source):
                raise DockerValidationError(
                    "a volume source is a named volume or a host path; names use letters, digits, '_', '.', '-'"
                )
            specs.append(MountSpec("volume", source, target, mode == "ro"))
    return specs


def label_pairs(values: object) -> dict[str, str]:
    if values in (None, []):
        return {}
    if not isinstance(values, list) or len(values) > MAX_LABELS:
        raise DockerValidationError(
            f"labels must be a list of at most {MAX_LABELS} key=value items"
        )
    labels: dict[str, str] = {}
    for item in values:
        key, sep, val = str(item).partition("=")
        key = key.strip().lower()
        if not sep or not _LABEL_KEY.match(key):
            raise DockerValidationError("each label must look like key=value (lowercase key)")
        if key.startswith(RESERVED_LABEL_PREFIXES):
            raise DockerValidationError(
                "labels under dev.velune. and com.docker.compose. are reserved for Velune and Compose"
            )
        if len(val) > 1024 or "\x00" in val:
            raise DockerValidationError("a label value is too long")
        labels[key] = val
    return labels


def memory_bytes(value: object) -> int:
    match = _MEMORY.match(str(value).strip())
    if not match:
        raise DockerValidationError("memory must look like 512m or 2g")
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group("unit").lower()]
    total = int(match.group("n")) * factor
    if total < 6 * 1024 * 1024 or total > 1024**4:
        raise DockerValidationError("memory must be between 6m and 1024g")
    return total


def cpu_nanos(value: object) -> int:
    try:
        cpus = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise DockerValidationError("cpus must be a number") from None
    if not 0.01 <= cpus <= 256:
        raise DockerValidationError("cpus must be between 0.01 and 256")
    return int(cpus * 1_000_000_000)


def device_specs(values: object) -> list[str]:
    if values in (None, []):
        return []
    if not isinstance(values, list) or len(values) > 8:
        raise DockerValidationError("devices must be a short list of /dev/... paths")
    out = []
    for item in values:
        text = _text(item, "a device", 256)
        host = text.split(":", 1)[0]
        if not re.match(r"^/dev/[A-Za-z0-9_./-]+$", host) or ".." in host:
            raise DockerValidationError("a device must be a /dev/... path")
        out.append(text)
    return out


_CAPABILITY = re.compile(r"^[A-Z_]{2,32}$")


def capabilities(values: object) -> list[str]:
    if values in (None, []):
        return []
    if not isinstance(values, list) or len(values) > 16:
        raise DockerValidationError("cap_add must be a short list of capability names")
    out = []
    for item in values:
        text = str(item).strip().upper()
        if not _CAPABILITY.match(text):
            raise DockerValidationError("a capability looks like NET_ADMIN")
        out.append(text)
    return out


def is_windows_style(path: str) -> bool:
    return bool(PureWindowsPath(path).drive)
