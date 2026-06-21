"""Usage analytics, quota monitoring, and provider health commands.

Commands:
  velune usage   — rich usage summary with terminal graphs
  velune quota   — quota and budget tracking
  velune health  — real-time provider health status
"""

from __future__ import annotations

import typer
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from velune.cli import design

console = Console()

usage_cmd = typer.Typer(
    name="usage",
    help="Show token usage, costs, and analytics across providers and models.",
    no_args_is_help=False,
    invoke_without_command=True,
)

quota_cmd = typer.Typer(
    name="quota",
    help="Monitor provider quota utilization and spending budgets.",
    no_args_is_help=False,
    invoke_without_command=True,
)

health_cmd = typer.Typer(
    name="health",
    help="Check real-time provider health and uptime status.",
    no_args_is_help=False,
    invoke_without_command=True,
)


# ---------------------------------------------------------------------------
# velune usage
# ---------------------------------------------------------------------------

@usage_cmd.callback(invoke_without_command=True)
def usage_summary(
    days: int = typer.Option(30, "--days", "-d", help="Number of days to include"),
    provider: str = typer.Option("", "--provider", "-p", help="Filter by provider"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Display token usage and cost analytics."""
    from velune.telemetry.usage_tracker import get_tracker

    tracker = get_tracker()
    provider_data = tracker.get_provider_usage(days=days)
    model_data = tracker.get_model_usage(days=days)

    if provider and provider_data:
        provider_data = [p for p in provider_data if p.provider_id == provider]
        model_data = [m for m in model_data if m.provider_id == provider]

    if json_output:
        import json
        output = {
            "days": days,
            "providers": [
                {
                    "provider_id": p.provider_id,
                    "requests": p.requests,
                    "successes": p.successes,
                    "failures": p.failures,
                    "total_tokens": p.total_tokens,
                    "cost_usd": round(p.cost_usd, 6),
                    "success_rate": round(p.success_rate, 1),
                    "avg_latency_ms": round(p.avg_latency_ms, 1),
                }
                for p in provider_data
            ],
            "models": [
                {
                    "model_id": m.model_id,
                    "provider_id": m.provider_id,
                    "requests": m.requests,
                    "total_tokens": m.total_tokens,
                    "cost_usd": round(m.cost_usd, 6),
                }
                for m in model_data
            ],
        }
        typer.echo(json.dumps(output, indent=2))
        return

    if not provider_data and not model_data:
        console.print(
            Panel(
                f"[{design.MUTED}]No usage data found for the past {days} days.\n"
                f"Start using Velune and data will appear here.[/{design.MUTED}]",
                title=f"[bold {design.ACCENT}]Usage Analytics[/bold {design.ACCENT}]",
                border_style=design.FAINT,
            )
        )
        return

    console.print()
    console.print(
        f"[bold {design.ACCENT}]Usage Analytics[/bold {design.ACCENT}]"
        f"  [{design.MUTED}]last {days} days[/{design.MUTED}]"
    )
    console.print()

    # Provider summary table
    _render_provider_table(provider_data)

    # Terminal bar chart — provider token distribution
    if len(provider_data) > 1:
        console.print()
        _render_provider_bar_chart(provider_data)

    # Model usage table
    if model_data:
        console.print()
        _render_model_table(model_data[:15])

    # Model token bar chart
    if len(model_data) > 1:
        console.print()
        _render_model_bar_chart(model_data[:10])

    # Cost distribution
    total_cost = sum(p.cost_usd for p in provider_data)
    if total_cost > 0:
        console.print()
        _render_cost_distribution(provider_data)

    # Footer totals
    console.print()
    total_tokens = sum(p.total_tokens for p in provider_data)
    total_requests = sum(p.requests for p in provider_data)
    total_failures = sum(p.failures for p in provider_data)
    console.print(
        f"[{design.MUTED}]Totals · {total_requests} requests · "
        f"{_fmt_tokens(total_tokens)} tokens · "
        f"${total_cost:.4f} · "
        f"{total_failures} failures[/{design.MUTED}]"
    )


def _render_provider_table(provider_data: list) -> None:
    table = Table(
        title=f"[bold {design.ACCENT}]Provider Summary[/bold {design.ACCENT}]",
        border_style=design.FAINT,
        padding=(0, 1),
    )
    table.add_column("Provider", style=design.INFO, min_width=14)
    table.add_column("Requests", style=design.MUTED, justify="right")
    table.add_column("Tokens", style=design.MUTED, justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Success %", justify="right")
    table.add_column("Avg Latency", justify="right", style=design.MUTED)
    table.add_column("Models Used", style=design.MUTED)

    for p in provider_data:
        sr = p.success_rate
        sr_color = design.OK if sr >= 95 else (design.WARN if sr >= 80 else "red")
        cost_str = f"${p.cost_usd:.4f}" if p.cost_usd else "—"
        models_str = ", ".join(p.models_used[:2])
        if len(p.models_used) > 2:
            models_str += f" +{len(p.models_used) - 2}"
        table.add_row(
            p.provider_id,
            str(p.requests),
            _fmt_tokens(p.total_tokens),
            cost_str,
            f"[{sr_color}]{sr:.1f}%[/{sr_color}]",
            f"{p.avg_latency_ms:.0f}ms" if p.avg_latency_ms else "—",
            models_str or "—",
        )

    console.print(table)


def _render_model_table(model_data: list) -> None:
    table = Table(
        title=f"[bold {design.ACCENT}]Model Usage[/bold {design.ACCENT}]",
        border_style=design.FAINT,
        padding=(0, 1),
    )
    table.add_column("Model", style=design.INFO, min_width=28)
    table.add_column("Provider", style=design.MUTED, min_width=12)
    table.add_column("Requests", style=design.MUTED, justify="right")
    table.add_column("Tokens", style=design.MUTED, justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Avg Latency", justify="right", style=design.MUTED)

    for m in model_data:
        cost_str = f"${m.cost_usd:.4f}" if m.cost_usd else "—"
        table.add_row(
            m.model_id,
            m.provider_id,
            str(m.requests),
            _fmt_tokens(m.total_tokens),
            cost_str,
            f"{m.avg_latency_ms:.0f}ms" if m.avg_latency_ms else "—",
        )

    console.print(table)


def _render_provider_bar_chart(provider_data: list) -> None:
    total = sum(p.total_tokens for p in provider_data) or 1
    console.print(f"[bold {design.ACCENT}]Provider Token Distribution[/bold {design.ACCENT}]")
    console.print()
    max_bar = 36

    for p in provider_data:
        pct = p.total_tokens / total
        bar_len = max(1, int(pct * max_bar)) if p.total_tokens else 0
        bar = "█" * bar_len
        pct_str = f"{pct * 100:.1f}%"
        line = (
            f"  [{design.INFO}]{p.provider_id:<14}[/{design.INFO}]"
            f"[{design.OK}]{bar}[/{design.OK}]"
            f"[{design.MUTED}] {pct_str}  ({_fmt_tokens(p.total_tokens)})[/{design.MUTED}]"
        )
        console.print(line)


def _render_model_bar_chart(model_data: list) -> None:
    total = sum(m.total_tokens for m in model_data) or 1
    console.print(f"[bold {design.ACCENT}]Model Token Distribution[/bold {design.ACCENT}]")
    console.print()
    max_bar = 28

    for m in model_data:
        pct = m.total_tokens / total
        bar_len = max(1, int(pct * max_bar)) if m.total_tokens else 0
        bar = "█" * bar_len
        # Trim model id for display
        mid = m.model_id
        if len(mid) > 26:
            mid = mid[:24] + "…"
        line = (
            f"  [{design.MUTED}]{mid:<26}[/{design.MUTED}]"
            f"[{design.INFO}]{bar}[/{design.INFO}]"
            f"[{design.MUTED}] {_fmt_tokens(m.total_tokens)}[/{design.MUTED}]"
        )
        console.print(line)


def _render_cost_distribution(provider_data: list) -> None:
    total_cost = sum(p.cost_usd for p in provider_data) or 0.0001
    console.print(f"[bold {design.ACCENT}]Cost Distribution[/bold {design.ACCENT}]")
    console.print()
    max_bar = 30

    for p in provider_data:
        if p.cost_usd <= 0:
            continue
        pct = p.cost_usd / total_cost
        bar_len = max(1, int(pct * max_bar))
        bar = "█" * bar_len
        line = (
            f"  [{design.INFO}]{p.provider_id:<14}[/{design.INFO}]"
            f"[{design.WARN}]{bar}[/{design.WARN}]"
            f"[{design.MUTED}] ${p.cost_usd:.4f}[/{design.MUTED}]"
        )
        console.print(line)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ---------------------------------------------------------------------------
# velune quota
# ---------------------------------------------------------------------------

@quota_cmd.callback(invoke_without_command=True)
def quota_overview(
    days: int = typer.Option(30, "--days", "-d", help="Days to include in current period"),
    budget: float = typer.Option(
        0.0, "--budget", "-b", help="Monthly budget limit in USD (0 = no limit)"
    ),
) -> None:
    """Display quota and budget utilization per provider."""
    from velune.telemetry.usage_tracker import get_tracker

    tracker = get_tracker()
    provider_data = tracker.get_provider_usage(days=days)
    total_cost = sum(p.cost_usd for p in provider_data)

    console.print()
    console.print(f"[bold {design.ACCENT}]Quota & Budget[/bold {design.ACCENT}]  "
                  f"[{design.MUTED}]last {days} days[/{design.MUTED}]")
    console.print()

    if not provider_data:
        console.print(
            f"[{design.MUTED}]No usage data. Start using Velune to see quota tracking.[/{design.MUTED}]"
        )
        return

    # Global budget meter
    if budget > 0:
        used_pct = min(total_cost / budget, 1.0)
        bar_len = int(used_pct * 30)
        remaining = max(0.0, budget - total_cost)
        bar_color = design.OK if used_pct < 0.7 else (design.WARN if used_pct < 0.9 else "red")
        bar = "█" * bar_len + "░" * (30 - bar_len)
        console.print(f"[bold {design.ACCENT}]Monthly Budget: ${budget:.2f}[/bold {design.ACCENT}]")
        console.print(
            f"  Used:      [bold]${total_cost:.4f}[/bold]  "
            f"Remaining: [bold {design.OK}]${remaining:.4f}[/bold {design.OK}]"
        )
        console.print(f"  [{bar_color}]{bar}[/{bar_color}] {used_pct * 100:.1f}%")
        console.print()

    # Per-provider quota table
    table = Table(border_style=design.FAINT, padding=(0, 1))
    table.add_column("Provider", style=design.INFO, min_width=14)
    table.add_column("Requests", justify="right", style=design.MUTED)
    table.add_column("Tokens", justify="right", style=design.MUTED)
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Failures", justify="right", style=design.MUTED)
    table.add_column("Utilization")

    max_tokens = max((p.total_tokens for p in provider_data), default=1) or 1

    for p in provider_data:
        pct = p.total_tokens / max_tokens
        bar_len = max(0, int(pct * 15))
        bar = "█" * bar_len + "░" * (15 - bar_len)
        color = design.OK if pct < 0.7 else (design.WARN if pct < 0.9 else "red")
        cost_str = f"${p.cost_usd:.4f}" if p.cost_usd else "$0.0000"
        table.add_row(
            p.provider_id,
            str(p.requests),
            _fmt_tokens(p.total_tokens),
            cost_str,
            str(p.failures) if p.failures else "—",
            f"[{color}]{bar}[/{color}] {pct * 100:.0f}%",
        )

    console.print(table)

    # Rate limit note
    console.print(
        f"\n[{design.MUTED}]Rate limit data shown when available from provider APIs.\n"
        f"Use `velune provider test` to check current API status.[/{design.MUTED}]"
    )


# ---------------------------------------------------------------------------
# velune health
# ---------------------------------------------------------------------------

@health_cmd.callback(invoke_without_command=True)
def health_overview(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full diagnostic"),
) -> None:
    """Check real-time health of all configured providers."""
    from velune.providers.keystore import has_key, is_ollama_live
    from velune.providers.validation import validate_provider_sync

    from velune.cli.commands.providers import _PROVIDER_META

    console.print()
    console.print(f"[bold {design.ACCENT}]Provider Health[/bold {design.ACCENT}]")
    console.print()

    to_check = []
    for pid, meta in _PROVIDER_META.items():
        if meta.get("local"):
            to_check.append((pid, meta, ""))
        elif has_key(pid):
            from velune.providers.keystore import get_key
            to_check.append((pid, meta, get_key(pid) or ""))

    if not to_check:
        console.print(
            f"[{design.WARN}]No providers configured. Run `velune setup` first.[/{design.WARN}]"
        )
        return

    table = Table(border_style=design.FAINT, padding=(0, 1))
    table.add_column("Provider", style=design.INFO, min_width=14)
    table.add_column("Status", min_width=20)
    table.add_column("Models", style=design.MUTED, justify="right", width=8)
    if verbose:
        table.add_column("Detail", style=design.MUTED)

    healthy = 0
    degraded = 0
    unavailable = 0

    for pid, meta, key in to_check:
        with console.status(f"  [{design.MUTED}]Checking {pid}...[/{design.MUTED}]"):
            result = validate_provider_sync(pid, key)

        if result.ok:
            healthy += 1
            status_str = f"[{design.OK}]✓ Healthy[/{design.OK}]"
            model_count = str(len(result.models)) if result.models else "—"
            detail = "OK"
        else:
            from velune.providers.validation import ValidationStatus
            if result.status == ValidationStatus.RATE_LIMITED:
                degraded += 1
                status_str = f"[{design.WARN}]⚠ Rate Limited[/{design.WARN}]"
                detail = "Rate limit exceeded"
            elif result.status == ValidationStatus.NETWORK_ERROR:
                degraded += 1
                status_str = f"[{design.WARN}]⚠ Network Error[/{design.WARN}]"
                detail = "Cannot reach provider"
            else:
                unavailable += 1
                status_str = f"[red]✗ {result.status.value.replace('_', ' ').title()}[/red]"
                detail = result.message[:50]
            model_count = "—"

        row = [pid, status_str, model_count]
        if verbose:
            row.append(detail)
        table.add_row(*row)

    console.print(table)

    # Summary footer
    total = healthy + degraded + unavailable
    console.print()
    console.print(
        f"  [{design.OK}]✓ {healthy} healthy[/{design.OK}]  "
        f"[{design.WARN}]⚠ {degraded} degraded[/{design.WARN}]  "
        f"[red]✗ {unavailable} unavailable[/red]  "
        f"[{design.MUTED}]({total} total)[/{design.MUTED}]"
    )

    if unavailable > 0:
        console.print(
            f"\n[{design.MUTED}]Run `velune provider test <name>` for detailed diagnostics.[/{design.MUTED}]"
        )


# ---------------------------------------------------------------------------
# Standalone command entry points (for velune usage / velune quota / velune health)
# ---------------------------------------------------------------------------

def usage_command(
    days: int = typer.Option(30, "--days", "-d"),
    provider: str = typer.Option("", "--provider", "-p"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show provider usage analytics (alias for `velune usage`)."""
    usage_summary(days=days, provider=provider, json_output=json_output)


def quota_command(
    days: int = typer.Option(30, "--days", "-d"),
    budget: float = typer.Option(0.0, "--budget", "-b"),
) -> None:
    """Show quota and budget utilization (alias for `velune quota`)."""
    quota_overview(days=days, budget=budget)


def health_command(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check provider health (alias for `velune health`)."""
    health_overview(verbose=verbose)
