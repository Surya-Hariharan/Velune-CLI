"""Live progress dashboard: background jobs, alerts, and provider health in one view."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from velune.core.task_registry import JobRegistry
    from velune.proactive.alerts import AlertStore

_STATUS_STYLE: dict[str, str] = {
    "running": "yellow",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim",
    "pending": "cyan",
}

_SEV_COLOR: dict[str, str] = {
    "danger": "red",
    "warn": "yellow",
    "info": "dim",
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
    # Rich builders
    # ------------------------------------------------------------------

    def _build_jobs_table(self) -> Table:
        table = Table(
            title="[bold cyan]Background Jobs[/bold cyan]",
            border_style="dim",
            padding=(0, 1),
            show_lines=False,
            expand=True,
        )
        table.add_column("ID", style="cyan", no_wrap=True, width=10)
        table.add_column("Task", max_width=40)
        table.add_column("Status", width=11)
        table.add_column("Phase", style="dim", width=16)
        table.add_column("Elapsed", justify="right", width=8)
        table.add_column("Result", style="dim", max_width=30)

        if self._jobs is None:
            table.add_row("—", "[dim]job registry unavailable[/dim]", "", "", "", "")
            return table

        jobs = self._jobs.all_jobs()
        if not jobs:
            table.add_row("—", "[dim]no jobs yet — use /run --bg[/dim]", "", "", "", "")
            return table

        for job in jobs:
            elapsed_s = (job.completed_at or time.monotonic()) - job.submitted_at
            st = job.status.value
            style = _STATUS_STYLE.get(st, "dim")
            result = job.result_preview or job.error or "—"
            table.add_row(
                job.job_id,
                job.name,
                f"[{style}]{st}[/{style}]",
                job.current_phase or "—",
                f"{elapsed_s:.0f}s",
                result[:30],
            )
        return table

    def _build_alerts_panel(self) -> Panel:
        if self._alerts is None:
            body = "[dim]alert store unavailable[/dim]"
        else:
            alerts = self._alerts.all_alerts()
            if not alerts:
                body = "[dim]No active alerts[/dim]"
            else:
                lines: list[str] = []
                for a in alerts[:10]:
                    color = _SEV_COLOR.get(a.severity.value, "dim")
                    lines.append(f"[{color}]{a.title}[/{color}]  [dim]{a.body[:60]}[/dim]")
                body = "\n".join(lines)
        return Panel(
            body,
            title="[bold yellow]Alerts[/bold yellow]",
            border_style="yellow",
        )

    def _build_health_panel(self) -> Panel:
        if self._health is None:
            body = "[dim]Health monitor not available[/dim]"
        else:
            try:
                manifests = self._health.get_all_manifests()
                if not manifests:
                    body = "[dim]No providers registered[/dim]"
                else:
                    lines: list[str] = []
                    for pid, m in manifests.items():
                        health_val = m.health.value if hasattr(m.health, "value") else str(m.health)
                        color = {
                            "healthy": "green",
                            "degraded": "yellow",
                            "unavailable": "red",
                        }.get(health_val, "dim")
                        latency = getattr(m, "estimated_latency_ms", None)
                        lat_str = f"[dim]{latency}ms[/dim]" if latency else ""
                        lines.append(f"[{color}]{pid}[/{color}]  {health_val}  {lat_str}")
                    body = "\n".join(lines)
            except Exception as exc:
                body = f"[dim]Health check error: {exc}[/dim]"
        return Panel(
            body,
            title="[bold cyan]Provider Health[/bold cyan]",
            border_style="cyan",
        )

    def _build_layout(self) -> Layout:
        layout = Layout()
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

        self._console.print("[dim]Dashboard active — press Enter to exit[/dim]")
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
