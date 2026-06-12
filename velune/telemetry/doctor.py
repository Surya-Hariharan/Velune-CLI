"""Doctor integration for telemetry and usage analytics."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from velune.telemetry.usage_tracker import get_tracker

logger = structlog.get_logger()


def print_provider_health_report(console: Console | None = None) -> dict[str, Any]:
    """Print provider health and capability manifest report.

    Returns:
        Dictionary with provider health data
    """
    if console is None:
        console = Console()

    try:
        from velune.kernel.registry import get_container
        container = get_container()
        if not container.has("runtime.provider_health_monitor"):
            return {"providers": []}
        monitor = container.get("runtime.provider_health_monitor")
    except (ImportError, AttributeError, KeyError):
        return {"providers": []}

    manifests = monitor.get_all_manifests()

    # Print header
    console.print(
        Panel(
            "[bold cyan]🏥 Provider Health[/bold cyan]",
            expand=False,
        )
    )

    if not manifests:
        console.print("[dim]No provider manifests available[/dim]")
        return {"providers": []}

    # Create health table
    table = Table(title=None, show_header=True)
    table.add_column("Provider", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Latency", justify="right", style="yellow")
    table.add_column("Models", justify="right", style="blue")
    table.add_column("Rate Limit", style="magenta")

    provider_data = []
    for provider_id, manifest in sorted(manifests.items()):
        from velune.core.types.provider import ProviderHealth

        # Format status with icon
        if manifest.health == ProviderHealth.HEALTHY:
            status_str = "[green]✓ HEALTHY[/green]"
        elif manifest.health == ProviderHealth.DEGRADED:
            status_str = "[yellow]⚠ DEGRADED[/yellow]"
        elif manifest.health == ProviderHealth.UNAVAILABLE:
            status_str = "[red]✗ OFFLINE[/red]"
        else:
            status_str = "[dim]? UNKNOWN[/dim]"

        # Format latency
        if manifest.estimated_latency_ms > 0:
            latency_str = f"{manifest.estimated_latency_ms}ms"
        else:
            latency_str = "—"

        # Format model count
        model_count = len(manifest.available_models)
        models_str = str(model_count) if model_count > 0 else "—"

        # Format rate limit
        if manifest.rate_limit_remaining is not None:
            if manifest.rate_limit_reset_at:
                import time
                reset_delta = int(manifest.rate_limit_reset_at - time.time())
                reset_min = max(0, reset_delta // 60)
                limit_str = f"{manifest.rate_limit_remaining} remaining (↻ {reset_min}m)"
            else:
                limit_str = f"{manifest.rate_limit_remaining} remaining"
        else:
            limit_str = "N/A (local)"

        table.add_row(provider_id, status_str, latency_str, models_str, limit_str)
        provider_data.append({
            "provider": provider_id,
            "health": str(manifest.health),
            "latency_ms": manifest.estimated_latency_ms,
            "model_count": model_count,
            "rate_limit": manifest.rate_limit_remaining,
        })

    console.print(table)
    return {"providers": provider_data}


def print_telemetry_report(console: Console | None = None) -> dict[str, Any]:
    """Print telemetry and recent activity report for 'velune doctor'.

    Returns:
        Dictionary with report data for structured analysis
    """
    if console is None:
        console = Console()

    tracker = get_tracker()

    # Get stats for last 7 days
    stats = tracker.get_stats_last_n_days(days=7)

    # Get recent sessions
    recent_sessions = tracker.get_recent_sessions(days=7)

    # Print header
    console.print(
        Panel(
            "[bold cyan]📊 Telemetry & Usage (Last 7 Days)[/bold cyan]",
            expand=False,
        )
    )

    # Print summary stats
    summary_lines = [
        f"[green]Sessions:[/green] {stats['session_count']}",
        f"[green]Total Tokens:[/green] {stats['total_tokens']:,}",
        f"[green]Completions:[/green] {stats['completion_count']}",
    ]

    if stats["total_cost"]:
        summary_lines.append(f"[green]Estimated Cost:[/green] ${stats['total_cost']:.2f}")

    if stats["most_used_model"]:
        summary_lines.append(
            f"[green]Most Used Model:[/green] {stats['most_used_model']} "
            f"({stats['most_used_model_tokens']:,} tokens)"
        )

    console.print("\n".join(summary_lines))

    # Print recent sessions table if any
    if recent_sessions:
        console.print("\n[bold]Recent Sessions:[/bold]")

        table = Table(title=None, show_header=True)
        table.add_column("Session ID", style="cyan")
        table.add_column("Start Time", style="green")
        table.add_column("Tokens", justify="right", style="yellow")
        table.add_column("Cost", justify="right", style="magenta")
        table.add_column("Models", style="blue")

        for session in recent_sessions[:10]:  # Show last 10
            # Parse start time
            start_time = datetime.fromisoformat(session.start_time)
            time_str = start_time.strftime("%Y-%m-%d %H:%M")

            # Format models
            models_str = ", ".join(
                f"{m}({t:,})" for m, t in session.model_breakdown.items()
            )

            # Format cost
            cost_str = f"${session.total_cost:.2f}" if session.total_cost else "—"

            table.add_row(
                session.session_id[:8],
                time_str,
                str(session.total_tokens),
                cost_str,
                models_str,
            )

        console.print(table)

    # Print average metrics
    if stats["session_count"] > 0:
        avg_tokens_per_session = stats["total_tokens"] // stats["session_count"]
        avg_cost_per_session = (
            stats["total_cost"] / stats["session_count"]
            if stats["total_cost"]
            else None
        )

        console.print("\n[bold]Averages per Session:[/bold]")
        avg_lines = [f"  Tokens: {avg_tokens_per_session:,}"]
        if avg_cost_per_session:
            avg_lines.append(f"  Cost: ${avg_cost_per_session:.2f}")
        console.print("\n".join(avg_lines))

    # Print log location
    from velune.telemetry.logging import get_log_directory

    log_dir = get_log_directory()
    console.print(f"\n[dim]Logs: {log_dir}[/dim]")

    # Return structured data
    return {
        "telemetry": {
            "sessions_7d": stats["session_count"],
            "total_tokens_7d": stats["total_tokens"],
            "total_cost_7d": stats["total_cost"],
            "completions_7d": stats["completion_count"],
            "most_used_model": stats["most_used_model"],
            "avg_tokens_per_session": (
                stats["total_tokens"] // stats["session_count"]
                if stats["session_count"] > 0
                else 0
            ),
        },
        "recent_sessions": [
            {
                "session_id": s.session_id,
                "tokens": s.total_tokens,
                "cost": s.total_cost,
                "models": list(s.model_breakdown.keys()),
            }
            for s in recent_sessions[:10]
        ],
    }


def get_telemetry_status() -> dict[str, Any]:
    """Get telemetry status for health checks.

    Returns:
        Dictionary with status and counts
    """
    tracker = get_tracker()
    stats = tracker.get_stats_last_n_days(days=7)

    return {
        "healthy": True,
        "sessions_tracked": stats["session_count"],
        "tokens_tracked": stats["total_tokens"],
        "estimated_cost": stats["total_cost"],
        "database": str(tracker.db_path),
    }
