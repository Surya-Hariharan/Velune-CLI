"""System dashboard: session, state, background jobs, alerts and provider health.

Rendered once per ``/dashboard`` call as an ordinary Rich renderable. There is
deliberately no ``Live`` view and no stdin reader: inside the full-screen REPL
the console is not interactive (``Live`` draws nothing until it stops) and
prompt_toolkit owns the keyboard (a second reader steals keystrokes). Running
``/dashboard`` again shows fresh numbers.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from velune.cli import design
from velune.cli.statusbar import _context_bar

if TYPE_CHECKING:
    from velune.cli.display.system_snapshot import LiveSessionState, SystemSnapshot
    from velune.core.task_registry import JobRegistry
    from velune.proactive.alerts import AlertStore

_STATUS_COLOR: dict[str, str] = {
    "running": design.WARN,
    "completed": design.OK,
    "failed": design.DANGER,
    "cancelled": design.FAINT,
    "pending": design.INFO,
}

_SEV_COLOR: dict[str, str] = {
    "danger": design.DANGER,
    "warn": design.WARN,
    "info": design.FAINT,
}

_HEALTH_COLOR: dict[str, str] = {
    "healthy": design.OK,
    "degraded": design.WARN,
    "unavailable": design.DANGER,
}

_FRESHNESS_COLOR: dict[str, str] = {
    "synced": design.OK,
    "stale": design.WARN,
    "unknown": design.FAINT,
    "no-index": design.FAINT,
}


def _panel(body: RenderableType, title: str, title_color: str, border: str = design.FAINT) -> Panel:
    return Panel(
        body,
        title=f"[bold {title_color}]{title}[/]",
        border_style=border,
        box=box.ROUNDED,
        padding=design.PADDING_COMPACT,
    )


def _health(manifest: Any) -> str:
    """``"<colored status>  <latency>"`` for one provider manifest."""
    value = manifest.health.value if hasattr(manifest.health, "value") else str(manifest.health)
    latency = getattr(manifest, "estimated_latency_ms", None)
    lat = f"  [{design.FAINT}]{latency}ms[/]" if latency else ""
    return f"[{_HEALTH_COLOR.get(value, design.FAINT)}]{value}[/]{lat}"


def _manifests(health_monitor: Any | None) -> dict[str, Any] | None:
    if health_monitor is None:
        return None
    try:
        return health_monitor.get_all_manifests()
    except Exception:
        return None


def _session_panel(
    live: LiveSessionState | None, snap: SystemSnapshot | None, manifests: dict[str, Any] | None
) -> Panel:
    provider_id = live.provider_id if live else None
    if not provider_id:
        provider = f"[{design.FAINT}]no provider[/]"
    else:
        manifest = (manifests or {}).get(provider_id)
        provider = f"[{design.WHITE}]{provider_id}[/]"
        if manifest is not None:
            provider += f"  {_health(manifest)}"
    model = (live.model_id if live else None) or "no model"
    mode = live.mode_label if live else "NORMAL"

    body = Text.from_markup(
        f"{provider}  {design.SEP}  [{design.WHITE}]{model}[/]  {design.SEP}  [{design.MUTED}]{mode}[/]"
    )
    line2 = Text()
    if snap is not None:
        line2.append(snap.workspace, style=design.MUTED)
        if snap.git_branch:
            dirty = snap.working_tree_dirty
            state = "✓clean" if dirty == 0 else (f"{dirty} dirty" if dirty > 0 else "")
            line2.append(f"  git:{snap.git_branch} {state}".rstrip(), style=design.FAINT)
    if live is not None:
        pct = live.context_pct
        color = design.OK if pct < 70 else (design.WARN if pct < 90 else design.DANGER)
        line2.append(f"   ctx {_context_bar(pct)} {pct:.0f}%", style=color)
    if line2:
        body.append("\n")
        body.append_text(line2)
    return _panel(body, "Session", design.ACCENT)


def _index_panel(snap: SystemSnapshot | None) -> Panel:
    if snap is None or not snap.index.exists:
        body = f"[{design.FAINT}]not indexed — run /index[/]"
    else:
        ix = snap.index
        body = (
            f"[{_FRESHNESS_COLOR.get(ix.freshness, design.FAINT)}]{ix.freshness}[/]\n"
            f"[{design.WHITE}]{ix.files}[/] [{design.FAINT}]files[/]  "
            f"[{design.WHITE}]{ix.symbols}[/] [{design.FAINT}]symbols[/]"
        )
    return _panel(body, "Index", design.INFO)


def _memory_panel(snap: SystemSnapshot | None) -> Panel:
    tables = snap.memory_tables if snap else []
    if not tables:
        body = f"[{design.FAINT}]no records yet[/]"
    else:
        top = sorted(tables, key=lambda t: t.rows, reverse=True)[:3]
        body = "\n".join(f"[{design.WHITE}]{t.rows}[/] [{design.FAINT}]{t.table}[/]" for t in top)
    return _panel(body, "Memory", design.INFO)


def _integrations_panel(snap: SystemSnapshot | None) -> Panel:
    if snap is None:
        body = f"[{design.FAINT}]unavailable[/]"
    else:
        ig = snap.integrations
        body = (
            f"[{design.WHITE}]{ig.plugins}[/] [{design.FAINT}]plugins[/]  "
            f"[{design.WHITE}]{ig.mcp_servers}[/] [{design.FAINT}]mcp[/]\n"
            f"[{design.WHITE}]{ig.sessions}[/] [{design.FAINT}]sessions[/]  "
            f"[{design.FAINT}]config {'✓' if ig.config_exists else '—'}[/]"
        )
    return _panel(body, "Integrations", design.INFO)


def _jobs_table(jobs: JobRegistry | None) -> Table:
    table = Table(
        title=f"[bold {design.WHITE}]Background Jobs[/]",
        box=box.SIMPLE_HEAD,
        border_style=design.FAINT,
        padding=design.PADDING_COMPACT,
        show_edge=False,
        expand=True,
    )
    table.add_column("ID", style=design.MUTED, no_wrap=True)
    table.add_column("Task", style=design.WHITE, overflow="ellipsis", ratio=3)
    table.add_column("Status", no_wrap=True)
    table.add_column("Phase", style=design.FAINT, overflow="ellipsis", ratio=1)
    table.add_column("Elapsed", justify="right", no_wrap=True)
    table.add_column("Result", style=design.MUTED, overflow="ellipsis", ratio=2)

    all_jobs = jobs.all_jobs() if jobs is not None else None
    if not all_jobs:
        note = "job registry unavailable" if jobs is None else "no jobs yet — use /council --bg"
        table.add_row("—", f"[{design.FAINT}]{note}[/]", "", "", "", "")
        return table
    for job in all_jobs:
        elapsed_s = (job.completed_at or time.monotonic()) - job.submitted_at
        status = job.status.value
        table.add_row(
            job.job_id,
            job.name,
            f"[{_STATUS_COLOR.get(status, design.FAINT)}]{status}[/]",
            job.current_phase or "—",
            f"{elapsed_s:.0f}s",
            job.result_preview or job.error or "—",
        )
    return table


def _alerts_panel(alerts: AlertStore | None) -> Panel:
    if alerts is None:
        body = f"[{design.FAINT}]alert store unavailable[/]"
    else:
        items = alerts.all_alerts()[:10]
        body = (
            "\n".join(
                f"[{_SEV_COLOR.get(a.severity.value, design.FAINT)}]{a.title}[/]  "
                f"[{design.FAINT}]{a.body[:60]}[/]"
                for a in items
            )
            or f"[{design.FAINT}]No active alerts[/]"
        )
    return _panel(body, "Alerts", design.WARN, border=design.WARN)


def _health_panel(health_monitor: Any | None, manifests: dict[str, Any] | None) -> Panel:
    if health_monitor is None:
        body = f"[{design.FAINT}]Health monitor not available[/]"
    elif manifests is None:
        body = f"[{design.FAINT}]Health check failed[/]"
    elif not manifests:
        body = f"[{design.FAINT}]No providers registered[/]"
    else:
        body = "\n".join(f"[{design.WHITE}]{pid}[/]  {_health(m)}" for pid, m in manifests.items())
    return _panel(body, "Provider Health", design.INFO, border=design.INFO)


def _row(*panels: Panel) -> Table:
    """Panels side by side, sharing the width equally."""
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in panels:
        grid.add_column(ratio=1)
    grid.add_row(*panels)
    return grid


def build_dashboard(
    *,
    jobs: JobRegistry | None = None,
    alerts: AlertStore | None = None,
    health_monitor: Any | None = None,
    snapshot: SystemSnapshot | None = None,
    live: LiveSessionState | None = None,
) -> Group:
    """The whole dashboard as one renderable; every source may be ``None``."""
    manifests = _manifests(health_monitor)
    return Group(
        _session_panel(live, snapshot, manifests),
        _row(_index_panel(snapshot), _memory_panel(snapshot), _integrations_panel(snapshot)),
        _jobs_table(jobs),
        _row(_alerts_panel(alerts), _health_panel(health_monitor, manifests)),
    )
