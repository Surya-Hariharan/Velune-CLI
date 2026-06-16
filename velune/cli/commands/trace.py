"""``velune trace`` — replay the real execution event stream.

Every command, indexing pass, tool launch, and orchestration milestone emits an
event on the :class:`~velune.events.CognitiveBus`; a session-side
:class:`~velune.observability.trace_sink.TraceSink` persists those events to a
bounded, secret-redacted log. This command reads them back so an operator can
*verify* what Velune actually did — with timestamps, sources, and correlation
ids — rather than trusting a black box.

It shows only events that were genuinely emitted. It never fabricates reasoning,
chain-of-thought, or hidden prompts; an empty log honestly reports "no events".

    velune trace            # recent events (default)
    velune trace recent     # same, with --limit / --type / --run filters
    velune trace live       # follow new events as they are written
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.table import Table
from rich.text import Text

from velune.cli import design
from velune.cli.context import CLIContext
from velune.observability.trace_log import TraceLog
from velune.observability.trace_sink import trace_log_for_workspace

console = Console()
trace_cmd = typer.Typer(help="View recent execution events (use 'live' to follow).")

# Coarse source → color mapping so the eye can group stages without noise.
_SOURCE_COLORS = {
    "planner": design.ACCENT_SOFT,
    "coder": design.OK,
    "reviewer": design.INFO,
    "executor": design.HIGHLIGHT,
    "execution": design.HIGHLIGHT,
    "sandbox": design.WARN,
    "memory": design.INFO,
    "retrieval": design.ACCENT_SOFT,
    "indexer": design.ACCENT,
    "repository": design.ACCENT,
}


def _log_for(ctx: typer.Context) -> tuple[TraceLog, bool]:
    """Resolve the workspace trace log and json-mode flag from CLI context."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")
    return trace_log_for_workspace(cli_context.workspace), cli_context.json_mode


def _source_style(source: str) -> str:
    key = (source or "").lower()
    for name, color in _SOURCE_COLORS.items():
        if name in key:
            return color
    return design.MUTED


def _fmt_clock(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "--:--:--"


def _summarize_data(data: Any) -> str:
    """One-line, low-noise summary of an event's data payload."""
    if not isinstance(data, dict) or not data:
        return ""
    parts = []
    for k, v in list(data.items())[:4]:
        sval = str(v)
        if len(sval) > 48:
            sval = sval[:47] + "…"
        parts.append(f"{k}={sval}")
    return "  ".join(parts)


def _render_table(console: Console, records: list[dict], *, title: str) -> None:
    table = Table(
        box=ROUNDED,
        border_style=design.FAINT,
        header_style=f"bold {design.ACCENT_SOFT}",
        title=f"[bold {design.ACCENT}]{title}[/]",
        title_justify="left",
        padding=(0, 1),
        expand=True,
    )
    table.add_column("Time", style=design.FAINT, no_wrap=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Event", style="default", no_wrap=True)
    table.add_column("Detail", style=design.MUTED, overflow="ellipsis")
    for rec in records:
        row = _row(rec)
        table.add_row(*row)
    console.print(table)


def _row(rec: dict) -> tuple[Text, Text, Text, str]:
    source = str(rec.get("source", "?"))
    src_text = Text(source, style=_source_style(source))
    evt_text = Text(str(rec.get("event_type", "?")), style=design.INFO)
    return (
        Text(_fmt_clock(float(rec.get("timestamp", 0.0))), style=design.FAINT),
        src_text,
        evt_text,
        _summarize_data(rec.get("data")),
    )


@trace_cmd.callback(invoke_without_command=True)
def trace_main(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", "-n", help="Max events to show"),
    type_filter: str = typer.Option(
        "", "--type", "-t", help="Filter by event-type substring (case-insensitive)"
    ),
    run: str = typer.Option("", "--run", "-r", help="Filter by run/correlation id"),
) -> None:
    """Show recent execution events (default action)."""
    if ctx.invoked_subcommand is not None:
        return
    _show_recent(ctx, limit=limit, type_filter=type_filter, run=run)


@trace_cmd.command("recent")
def trace_recent(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", "-n", help="Max events to show"),
    type_filter: str = typer.Option(
        "", "--type", "-t", help="Filter by event-type substring (case-insensitive)"
    ),
    run: str = typer.Option("", "--run", "-r", help="Filter by run/correlation id"),
) -> None:
    """Show recent execution events from the persisted trace log."""
    _show_recent(ctx, limit=limit, type_filter=type_filter, run=run)


def _show_recent(ctx: typer.Context, *, limit: int, type_filter: str, run: str) -> None:
    log, json_mode = _log_for(ctx)
    records = log.read_recent(
        limit=limit,
        type_filter=type_filter or None,
        run_id=run or None,
    )

    if json_mode:
        print(json.dumps({"events": records, "total_stored": log.count()}))
        return

    if not records:
        console.print(
            f"[{design.MUTED}]No trace events recorded yet.[/] "
            f"[{design.FAINT}]Run a task (e.g. `velune ask ...`) and they will appear here.[/]"
        )
        return

    _render_table(console, records, title=f"Recent execution trace ({len(records)} events)")
    console.print(f"[{design.FAINT}]{log.count()} total events stored · {log.path}[/]")


@trace_cmd.command("live")
def trace_live(
    ctx: typer.Context,
    type_filter: str = typer.Option(
        "", "--type", "-t", help="Filter by event-type substring (case-insensitive)"
    ),
) -> None:
    """Follow new execution events as they are written (Ctrl+C to stop).

    This tails the persisted trace log, so it observes a *separate* running
    Velune session in real time. It does not emit anything itself.
    """
    log, json_mode = _log_for(ctx)

    # Establish the starting point so we only print genuinely new events.
    seen = {r.get("event_id") for r in log.read_recent(limit=10_000)}
    if not json_mode:
        console.print(
            f"[{design.MUTED}]Following execution trace[/] "
            f"[{design.FAINT}]({log.path}) — Ctrl+C to stop[/]"
        )
    try:
        while True:
            for rec in log.read_recent(limit=500, type_filter=type_filter or None):
                eid = rec.get("event_id")
                if eid in seen:
                    continue
                seen.add(eid)
                if json_mode:
                    print(json.dumps(rec), flush=True)
                else:
                    t, src, evt, detail = _row(rec)
                    line = Text()
                    line.append_text(t)
                    line.append("  ")
                    line.append_text(src)
                    line.append("  ")
                    line.append_text(evt)
                    if detail:
                        line.append("  ")
                        line.append(detail, style=design.MUTED)
                    console.print(line)
            time.sleep(0.5)
    except KeyboardInterrupt:
        if not json_mode:
            console.print(f"[{design.FAINT}]Stopped.[/]")
