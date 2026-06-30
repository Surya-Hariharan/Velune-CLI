"""Live progress dashboard: background jobs, alerts, and provider health in one view."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
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


class ProgressDashboard:
    """Rich Live dashboard showing background jobs, active alerts, and provider health.

    Refreshes every 500 ms; exits on Enter key or Ctrl+C.
    """

    REFRESH_INTERVAL_S: float = 0.5

    def __init__(
        self,
        console: Console,
        job_registry: JobRegistry | None,
        alert_store: AlertStore | None,
        health_monitor: Any | None = None,
        snapshot: SystemSnapshot | None = None,
        live_state: Callable[[], LiveSessionState] | None = None,
    ) -> None:
        self._console = console
        self._jobs = job_registry
        self._alerts = alert_store
        self._health = health_monitor
        # Static system snapshot — built once by the caller and reused across the
        # 500 ms refresh loop so the live view never re-reads disk mid-tick.
        self._snapshot = snapshot
        # Fast-changing session fields (model/mode/context) — re-read every tick.
        self._live_state = live_state

    # ------------------------------------------------------------------
    # Rich builders — all colours from design tokens
    # ------------------------------------------------------------------

    def _build_jobs_table(self) -> Table:
        table = Table(
            title=f"[bold {design.WHITE}]Background Jobs[/]",
            box=box.SIMPLE_HEAD,
            border_style=design.FAINT,
            padding=design.PADDING_COMPACT,
            show_lines=False,
            show_edge=False,
            expand=True,
        )
        table.add_column("ID", style=design.MUTED, no_wrap=True, width=10)
        table.add_column("Task", style=design.WHITE, max_width=40)
        table.add_column("Status", width=11)
        table.add_column("Phase", style=design.FAINT, width=16)
        table.add_column("Elapsed", justify="right", width=8)
        table.add_column("Result", style=design.MUTED, max_width=30)

        if self._jobs is None:
            table.add_row("—", f"[{design.FAINT}]job registry unavailable[/]", "", "", "", "")
            return table

        jobs = self._jobs.all_jobs()
        if not jobs:
            table.add_row("—", f"[{design.FAINT}]no jobs yet — use /run --bg[/]", "", "", "", "")
            return table

        for job in jobs:
            elapsed_s = (job.completed_at or time.monotonic()) - job.submitted_at
            st = job.status.value
            color = _STATUS_COLOR.get(st, design.FAINT)
            result = job.result_preview or job.error or "—"
            table.add_row(
                job.job_id,
                job.name,
                f"[{color}]{st}[/]",
                job.current_phase or "—",
                f"{elapsed_s:.0f}s",
                result[:30],
            )
        return table

    def _build_alerts_panel(self) -> Panel:
        if self._alerts is None:
            body = f"[{design.FAINT}]alert store unavailable[/]"
        else:
            alerts = self._alerts.all_alerts()
            if not alerts:
                body = f"[{design.FAINT}]No active alerts[/]"
            else:
                lines: list[str] = []
                for a in alerts[:10]:
                    color = _SEV_COLOR.get(a.severity.value, design.FAINT)
                    lines.append(f"[{color}]{a.title}[/]  [{design.FAINT}]{a.body[:60]}[/]")
                body = "\n".join(lines)
        return Panel(
            body,
            title=f"[bold {design.WARN}]Alerts[/]",
            border_style=design.WARN,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )

    def _build_health_panel(self) -> Panel:
        if self._health is None:
            body = f"[{design.FAINT}]Health monitor not available[/]"
        else:
            _health_color = {
                "healthy": design.OK,
                "degraded": design.WARN,
                "unavailable": design.DANGER,
            }
            try:
                manifests = self._health.get_all_manifests()
                if not manifests:
                    body = f"[{design.FAINT}]No providers registered[/]"
                else:
                    lines: list[str] = []
                    for pid, m in manifests.items():
                        health_val = m.health.value if hasattr(m.health, "value") else str(m.health)
                        color = _health_color.get(health_val, design.FAINT)
                        latency = getattr(m, "estimated_latency_ms", None)
                        lat_str = f"[{design.FAINT}]{latency}ms[/]" if latency else ""
                        lines.append(f"[{color}]{pid}[/]  {health_val}  {lat_str}")
                    body = "\n".join(lines)
            except Exception as exc:
                body = f"[{design.FAINT}]Health check error: {exc}[/]"
        return Panel(
            body,
            title=f"[bold {design.INFO}]Provider Health[/]",
            border_style=design.INFO,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )

    # ------------------------------------------------------------------
    # Session band + State row — the "what is Velune doing?" surface
    # ------------------------------------------------------------------

    _FRESHNESS_COLOR: dict[str, str] = {
        "synced": design.OK,
        "stale": design.WARN,
        "unknown": design.FAINT,
        "no-index": design.FAINT,
    }

    def _provider_line(self, provider_id: str | None) -> str:
        """Health + latency for *provider_id*, read live from the health monitor."""
        if not provider_id:
            return f"[{design.FAINT}]no provider[/]"
        if self._health is None:
            return f"[{design.WHITE}]{provider_id}[/]"
        try:
            manifests = self._health.get_all_manifests()
            m = manifests.get(provider_id)
        except Exception:
            m = None
        if m is None:
            return f"[{design.WHITE}]{provider_id}[/]"
        health_val = m.health.value if hasattr(m.health, "value") else str(m.health)
        color = {
            "healthy": design.OK,
            "degraded": design.WARN,
            "unavailable": design.DANGER,
        }.get(health_val, design.FAINT)
        latency = getattr(m, "estimated_latency_ms", None)
        lat = f"  [{design.FAINT}]{latency}ms[/]" if latency else ""
        return f"[{design.WHITE}]{provider_id}[/]  [{color}]{health_val}[/]{lat}"

    def _build_session_panel(self) -> Panel:
        live = self._live_state() if self._live_state else None
        snap = self._snapshot

        model = (live.model_id if live and live.model_id else None) or "no model"
        mode = live.mode_label if live else "NORMAL"
        provider = self._provider_line(getattr(live, "provider_id", None) if live else None)

        line1 = Text.from_markup(
            f"{provider}  {design.SEP}  [{design.WHITE}]{model}[/]  "
            f"{design.SEP}  [{design.MUTED}]{mode}[/]"
        )

        line2 = Text()
        if snap is not None:
            line2.append(snap.workspace, style=design.MUTED)
            if snap.git_branch:
                clean = snap.working_tree_dirty
                git_state = f"git:{snap.git_branch} " + (
                    "✓clean" if clean == 0 else (f"{clean} dirty" if clean > 0 else "")
                )
                line2.append(f"  {git_state.strip()}", style=design.FAINT)
        if live is not None:
            pct = live.context_pct
            ctx_color = design.OK if pct < 70 else (design.WARN if pct < 90 else design.DANGER)
            line2.append(f"   ctx {_context_bar(pct)} ", style=ctx_color)
            line2.append(f"{pct:.0f}%", style=ctx_color)

        body = Text.assemble(line1, "\n", line2)
        return Panel(
            body,
            title=f"[bold {design.ACCENT}]Session[/]",
            border_style=design.FAINT,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )

    def _build_index_panel(self) -> Panel:
        snap = self._snapshot
        if snap is None or not snap.index.exists:
            body = f"[{design.FAINT}]not indexed — run /index[/]"
        else:
            ix = snap.index
            color = self._FRESHNESS_COLOR.get(ix.freshness, design.FAINT)
            body = (
                f"[{color}]{ix.freshness}[/]\n"
                f"[{design.WHITE}]{ix.files}[/] [{design.FAINT}]files[/]  "
                f"[{design.WHITE}]{ix.symbols}[/] [{design.FAINT}]symbols[/]"
            )
        return Panel(
            body,
            title=f"[bold {design.INFO}]Index[/]",
            border_style=design.FAINT,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )

    def _build_memory_panel(self) -> Panel:
        snap = self._snapshot
        tables = snap.memory_tables if snap else []
        if not tables:
            body = f"[{design.FAINT}]no records yet[/]"
        else:
            top = sorted(tables, key=lambda t: t.rows, reverse=True)[:3]
            lines = [f"[{design.WHITE}]{t.rows}[/] [{design.FAINT}]{t.table}[/]" for t in top]
            body = "\n".join(lines)
        return Panel(
            body,
            title=f"[bold {design.INFO}]Memory[/]",
            border_style=design.FAINT,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )

    def _build_integrations_panel(self) -> Panel:
        snap = self._snapshot
        if snap is None:
            body = f"[{design.FAINT}]unavailable[/]"
        else:
            ig = snap.integrations
            cfg = "✓" if ig.config_exists else "—"
            body = (
                f"[{design.WHITE}]{ig.plugins}[/] [{design.FAINT}]plugins[/]  "
                f"[{design.WHITE}]{ig.mcp_servers}[/] [{design.FAINT}]mcp[/]\n"
                f"[{design.WHITE}]{ig.sessions}[/] [{design.FAINT}]sessions[/]  "
                f"[{design.FAINT}]config {cfg}[/]"
            )
        return Panel(
            body,
            title=f"[bold {design.INFO}]Integrations[/]",
            border_style=design.FAINT,
            box=box.ROUNDED,
            padding=design.PADDING_COMPACT,
        )

    def _build_layout(self) -> Layout:
        layout = Layout()
        # The Session band + State row only appear when a snapshot was supplied,
        # so existing callers that construct a bare jobs/alerts/health dashboard
        # keep their original layout.
        rich_view = self._snapshot is not None or self._live_state is not None
        if rich_view:
            layout.split_column(
                Layout(name="session", size=4),
                Layout(name="state", size=4),
                Layout(name="jobs", ratio=2),
                Layout(name="bottom", ratio=1),
            )
            layout["session"].update(self._build_session_panel())
            layout["state"].split_row(
                Layout(name="index"),
                Layout(name="memory"),
                Layout(name="integrations"),
            )
            layout["index"].update(self._build_index_panel())
            layout["memory"].update(self._build_memory_panel())
            layout["integrations"].update(self._build_integrations_panel())
        else:
            layout.split_column(
                Layout(name="jobs", ratio=2),
                Layout(name="bottom", ratio=1),
            )
        layout["bottom"].split_row(
            Layout(name="alerts"),
            Layout(name="health"),
        )
        layout["jobs"].update(self._build_jobs_table())
        layout["alerts"].update(self._build_alerts_panel())
        layout["health"].update(self._build_health_panel())
        return layout

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run_until_keypress(self) -> None:
        """Show the live dashboard until the user presses Enter or Ctrl+C."""
        stop_event = asyncio.Event()

        async def _refresh_loop() -> None:
            with Live(
                self._build_layout(),
                console=self._console,
                refresh_per_second=int(1 / self.REFRESH_INTERVAL_S),
                screen=False,
            ) as live:
                while not stop_event.is_set():
                    live.update(self._build_layout())
                    await asyncio.sleep(self.REFRESH_INTERVAL_S)

        async def _wait_for_key() -> None:
            try:
                await asyncio.to_thread(_read_line)
            except Exception:
                pass
            stop_event.set()

        self._console.print(f"[{design.FAINT}]Dashboard active — press Enter to exit[/]")
        refresh_task = asyncio.create_task(_refresh_loop(), name="dashboard-refresh")
        key_task = asyncio.create_task(_wait_for_key(), name="dashboard-key-wait")
        try:
            await asyncio.wait(
                [refresh_task, key_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_event.set()
            refresh_task.cancel()
            key_task.cancel()
            for t in (refresh_task, key_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


def _read_line() -> str:
    """Blocking stdin read — runs in a thread via asyncio.to_thread."""
    import sys

    return sys.stdin.readline()
