"""Provider health report for `velune doctor providers`."""

from __future__ import annotations

from typing import Any

import structlog
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

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
            status_str = "[green]HEALTHY[/green]"
        elif manifest.health == ProviderHealth.DEGRADED:
            status_str = "[yellow]DEGRADED[/yellow]"
        elif manifest.health == ProviderHealth.UNAVAILABLE:
            status_str = "[red]OFFLINE[/red]"
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
        provider_data.append(
            {
                "provider": provider_id,
                "health": str(manifest.health),
                "latency_ms": manifest.estimated_latency_ms,
                "model_count": model_count,
                "rate_limit": manifest.rate_limit_remaining,
            }
        )

    console.print(table)
    return {"providers": provider_data}
