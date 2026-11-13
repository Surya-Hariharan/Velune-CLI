"""Turning Docker's raw API objects into small, redacted, model-safe records (VAIR A4).

The raw ``inspect`` JSON of a container is large and carries the environment
(where credentials usually live) and host paths. The model gets a curated view:

* environment variables appear **by name only**, never their values;
* labels are shown without the reserved ``dev.velune.*`` / compose bookkeeping
  clutter beyond a few useful keys, and are bounded;
* bind-mount host paths are shown (the person needs them to decide) but nothing
  from inside a container is read;
* log and output text is truncated from the *end* that matters and capped.

Nothing here talks to the daemon.
"""

from __future__ import annotations

from typing import Any

from velune.core.redaction import redact_secrets
from velune.resources.docker.ownership import (
    LABEL_MANAGED,
    LABEL_SESSION,
    LABEL_WORKSPACE,
    Facts,
)

MAX_LABELS_SHOWN = 15
MAX_LINE = 2000
_HIDDEN_LABELS = frozenset({LABEL_MANAGED, LABEL_SESSION, LABEL_WORKSPACE})


def short_id(value: str | None) -> str:
    text = str(value or "")
    return text.removeprefix("sha256:")[:12]


def shown_labels(labels: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in sorted((labels or {}).items()):
        if key in _HIDDEN_LABELS:
            continue
        out[str(key)[:100]] = redact_secrets(str(value))[:200]
        if len(out) >= MAX_LABELS_SHOWN:
            break
    return out


def env_names(env: list[str] | None) -> list[str]:
    """Variable *names* only — the values are never shown."""
    names = []
    for item in env or []:
        names.append(str(item).partition("=")[0][:100])
    return sorted(set(names))[:100]


def ports_view(ports: dict[str, Any] | None) -> list[str]:
    rows = []
    for container_port, bindings in sorted((ports or {}).items()):
        if not bindings:
            rows.append(f"{container_port} (not published)")
            continue
        for b in bindings:
            rows.append(f"{b.get('HostIp', '')}:{b.get('HostPort', '')}->{container_port}")
    return rows[:50]


def container_facts(attrs: dict[str, Any]) -> Facts:
    config = attrs.get("Config") or {}
    return Facts(
        "container",
        str(attrs.get("Id", "")),
        str(attrs.get("Name", "")).lstrip("/"),
        str(attrs.get("Created", "")),
        {str(k): str(v) for k, v in (config.get("Labels") or {}).items()},
    )


def image_facts(attrs: dict[str, Any]) -> Facts:
    tags = attrs.get("RepoTags") or []
    return Facts(
        "image",
        str(attrs.get("Id", "")),
        str(tags[0]) if tags else "",
        str(attrs.get("Id", "")),
        {},
    )


def volume_facts(attrs: dict[str, Any]) -> Facts:
    return Facts(
        "volume",
        str(attrs.get("Name", "")),
        str(attrs.get("Name", "")),
        str(attrs.get("CreatedAt", "")),
        {str(k): str(v) for k, v in (attrs.get("Labels") or {}).items()},
    )


def network_facts(attrs: dict[str, Any]) -> Facts:
    return Facts(
        "network",
        str(attrs.get("Id", "")),
        str(attrs.get("Name", "")),
        str(attrs.get("Created", "")),
        {str(k): str(v) for k, v in (attrs.get("Labels") or {}).items()},
    )


def container_row(attrs: dict[str, Any], owner: str = "") -> dict[str, Any]:
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    row: dict[str, Any] = {
        "id": short_id(attrs.get("Id")),
        "name": str(attrs.get("Name", "")).lstrip("/"),
        "image": str(config.get("Image", ""))[:200],
        "status": state.get("Status") if isinstance(state, dict) else str(state),
    }
    if owner:
        row["ownership"] = owner
    return row


def container_view(attrs: dict[str, Any], owner: str) -> dict[str, Any]:
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    network = attrs.get("NetworkSettings") or {}
    mounts = [
        {
            "type": m.get("Type"),
            "source": m.get("Source") if m.get("Type") == "bind" else m.get("Name"),
            "destination": m.get("Destination"),
            "read_only": not m.get("RW", True),
        }
        for m in (attrs.get("Mounts") or [])[:30]
    ]
    view: dict[str, Any] = {
        **container_row(attrs, owner),
        "created": attrs.get("Created"),
        "started_at": state.get("StartedAt") if isinstance(state, dict) else None,
        "exit_code": state.get("ExitCode") if isinstance(state, dict) else None,
        "restart_count": attrs.get("RestartCount"),
        "command": [str(a)[:200] for a in (config.get("Cmd") or [])][:20],
        "environment_variables": env_names(config.get("Env")),
        "ports": ports_view(network.get("Ports")),
        "networks": sorted((network.get("Networks") or {}).keys())[:20],
        "mounts": mounts,
        "labels": shown_labels(config.get("Labels")),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        "memory_limit": host.get("Memory") or None,
        "privileged": bool(host.get("Privileged")),
        "network_mode": host.get("NetworkMode"),
    }
    if host.get("Devices"):
        view["devices"] = [d.get("PathOnHost") for d in host["Devices"]][:10]
    if host.get("CapAdd"):
        view["cap_add"] = list(host["CapAdd"])[:20]
    health = (state.get("Health") if isinstance(state, dict) else None) or None
    if health:
        view["health"] = health.get("Status")
    return view


def image_row(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": short_id(attrs.get("Id")),
        "tags": list(attrs.get("RepoTags") or [])[:10],
        "size_mb": round(int(attrs.get("Size") or 0) / 1_000_000, 1),
        "created": attrs.get("Created"),
    }


def image_view(attrs: dict[str, Any], owner: str) -> dict[str, Any]:
    config = attrs.get("Config") or attrs.get("ContainerConfig") or {}
    return {
        **image_row(attrs),
        "ownership": owner,
        "digests": [str(d)[:120] for d in (attrs.get("RepoDigests") or [])][:5],
        "architecture": attrs.get("Architecture"),
        "os": attrs.get("Os"),
        "user": config.get("User") or None,
        "entrypoint": [str(a)[:200] for a in (config.get("Entrypoint") or [])][:10],
        "command": [str(a)[:200] for a in (config.get("Cmd") or [])][:10],
        "exposed_ports": sorted((config.get("ExposedPorts") or {}).keys())[:30],
        "environment_variables": env_names(config.get("Env")),
        "labels": shown_labels(config.get("Labels")),
    }


def volume_row(
    attrs: dict[str, Any], owner: str = "", attached: list[str] | None = None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": attrs.get("Name"),
        "driver": attrs.get("Driver"),
        "created": attrs.get("CreatedAt"),
        "labels": shown_labels(attrs.get("Labels")),
    }
    if owner:
        row["ownership"] = owner
    if attached is not None:
        row["attached_containers"] = attached[:20]
        row["in_use"] = bool(attached)
    return row


def network_row(attrs: dict[str, Any], owner: str = "") -> dict[str, Any]:
    attached = [str(c.get("Name", "")) for c in (attrs.get("Containers") or {}).values()]
    row = {
        "id": short_id(attrs.get("Id")),
        "name": attrs.get("Name"),
        "driver": attrs.get("Driver"),
        "scope": attrs.get("Scope"),
        "internal": bool(attrs.get("Internal")),
        "attached_containers": attached[:30],
        "in_use": bool(attached),
        "labels": shown_labels(attrs.get("Labels")),
    }
    if owner:
        row["ownership"] = owner
    return row


def bounded_text(
    data: bytes | str, *, max_chars: int, tail_lines: int | None = None
) -> dict[str, Any]:
    """Decode container output, keep the *end* (the recent part), cap it, redact secrets."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    lines = text.splitlines()
    truncated = False
    if tail_lines is not None and len(lines) > tail_lines:
        lines, truncated = lines[-tail_lines:], True
    lines = [line[:MAX_LINE] for line in lines]
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined, truncated = joined[-max_chars:], True
    return {"text": redact_secrets(joined), "lines": len(lines), "truncated": truncated}


def stats_view(raw: dict[str, Any]) -> dict[str, Any]:
    cpu = raw.get("cpu_stats") or {}
    pre = raw.get("precpu_stats") or {}
    mem = raw.get("memory_stats") or {}
    try:
        cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (
            pre.get("cpu_usage") or {}
        ).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        cpus = (
            cpu.get("online_cpus")
            or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
            or 1
        )
        cpu_percent = (
            round(cpu_delta / sys_delta * cpus * 100, 2)
            if sys_delta > 0 and cpu_delta >= 0
            else None
        )
    except (TypeError, ZeroDivisionError):
        cpu_percent = None
    usage, limit = mem.get("usage"), mem.get("limit")
    networks = raw.get("networks") or {}
    return {
        "cpu_percent": cpu_percent,
        "memory_usage_mb": round(usage / 1_000_000, 1) if isinstance(usage, (int, float)) else None,
        "memory_limit_mb": round(limit / 1_000_000, 1) if isinstance(limit, (int, float)) else None,
        "pids": (raw.get("pids_stats") or {}).get("current"),
        "network_rx_bytes": sum(n.get("rx_bytes", 0) for n in networks.values()),
        "network_tx_bytes": sum(n.get("tx_bytes", 0) for n in networks.values()),
    }
