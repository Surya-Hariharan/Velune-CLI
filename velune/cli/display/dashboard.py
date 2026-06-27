"""Live progress dashboard: background jobs, alerts, and provider health in one view."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from velune.cli import design

if TYPE_CHECKING:
    from velune.core.task_registry import JobRegistry
    from velune.proactive.alerts import AlertStore

_STATUS_COLOR: dict[str, str] = {
    "running":   design.WARN,
    "completed": design.OK,
    "failed":    design.DANGER,
    "cancelled": design.FAINT,
    "pending":   design.INFO,
}

_SEV_COLOR: dict[str, str] = {
    "danger": design.DANGER,
    "warn":   design.WARN,
    "info":   design.FAINT,
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
    ) -> None:
        self._console = console
        self._jobs = job_registry
        self._alerts = alert_store
        self._health = health_monitor

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
        table.add_column("ID",      style=design.MUTED,  no_wrap=True, width=10)
        table.add_column("Task",    style=design.WHITE,  max_width=40)
        table.add_column("Status",  width=11)
        table.add_column("Phase",   style=design.FAINT,  width=16)
        table.add_column("Elapsed", justify="right",     width=8)
        table.add_column("Result",  style=design.MUTED,  max_width=30)

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
                    lines.append(
                        f"[{color}]{a.title}[/]  [{design.FAINT}]{a.body[:60]}[/]"
                    )
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
                "healthy":     design.OK,
                "degraded":    design.WARN,
                "unavailable": design.DANGER,
            }
            try:
                manifests = self._health.get_all_manifests()
                if not manifests:
                    body = f"[{design.FAINT}]No providers registered[/]"
                else:
                    lines: list[str] = []
                    for pid, m in manifests.items():
                        health_val = (
                            m.health.value if hasattr(m.health, "value") else str(m.health)
                        )
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

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="jobs",   ratio=2),
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
