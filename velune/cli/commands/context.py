"""``velune context`` — prove the repository context is real and fresh.

This is the flagship operational-transparency command. It renders a truthful
snapshot of what Velune has actually indexed and persisted for the current
workspace: file/symbol counts by language, index freshness against the live git
HEAD, on-disk storage footprint, and cognitive-core record counts. Every number
comes from :func:`velune.observability.context_report.build_context_report`,
which reads real files and runs read-only git/SQLite queries — there are no
placeholders.

The command deliberately does not start the full runtime, so it stays fast on
large repositories and works even before the first session.
"""

from __future__ import annotations

import json

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from velune.cli import design
from velune.cli.context import CLIContext
from velune.observability.context_report import ContextReport, build_context_report
from velune.observability.format import human_bytes, relative_time

console = Console()
context_cmd = typer.Typer(help="Show index freshness, file counts, and workspace health.")

_FRESHNESS_STYLE = {
    "synced": (design.OK, "● synced"),
    "stale": (design.WARN, "● stale"),
    "unknown": (design.MUTED, "● unknown"),
    "no-index": (design.DANGER, "● not indexed"),
}

_STATE_GLYPH = {
    "ok": (design.OK, "✓"),
    "warn": (design.WARN, "!"),
    "danger": (design.DANGER, "✗"),
}


@context_cmd.callback(invoke_without_command=True)
def context_main(ctx: typer.Context) -> None:
    """Show the repository context report for the active workspace."""
    if ctx.invoked_subcommand is not None:
        return

    cli_context = ctx.obj
    workspace = cli_context.workspace if isinstance(cli_context, CLIContext) else None
    if workspace is None:
        raise typer.BadParameter("CLI context was not properly initialized")

    json_mode = isinstance(cli_context, CLIContext) and cli_context.json_mode
    report = build_context_report(workspace)

    if json_mode:
        print(json.dumps(report.to_dict()))
        return

    _render(console, report)


def _render(console: Console, report: ContextReport) -> None:
    """Render the context report as calm, infrastructure-grade panels."""
    # --- Header: workspace + git + freshness ---
    fresh_color, fresh_label = _FRESHNESS_STYLE.get(
        report.freshness, (design.MUTED, report.freshness)
    )
    header = Text()
    header.append("Workspace  ", style=design.MUTED)
    header.append(report.workspace + "\n", style=f"bold {design.ACCENT}")
    if report.git_branch:
        header.append("Branch     ", style=design.MUTED)
        sha = (report.head_sha or "")[:8]
        header.append(f"{report.git_branch}", style=design.INFO)
        if sha:
            header.append(f"  @ {sha}", style=design.FAINT)
        header.append("\n")
    header.append("Index      ", style=design.MUTED)
    header.append(fresh_label, style=f"bold {fresh_color}")
    header.append(f"   ·   indexed {relative_time(report.last_indexed_at)}", style=design.FAINT)
    console.print(Panel(header, box=ROUNDED, border_style=design.FAINT, padding=(1, 2)))

    # --- Index summary + language breakdown ---
    if report.index_exists and report.languages:
        lang_table = Table(
            box=ROUNDED,
            border_style=design.FAINT,
            header_style=f"bold {design.ACCENT_SOFT}",
            title=f"[bold {design.ACCENT}]Indexed code[/]  "
            f"[{design.MUTED}]{report.indexed_file_count} files · "
            f"{report.total_symbols} symbols[/]",
            title_justify="left",
            padding=(0, 1),
        )
        lang_table.add_column("Language", style=design.INFO)
        lang_table.add_column("Files", justify="right", style="default")
        lang_table.add_column("Symbols", justify="right", style=design.MUTED)
        for ls in report.languages:
            lang_table.add_row(ls.language, str(ls.files), str(ls.symbols))
        console.print(lang_table)
    else:
        console.print(
            f"[{design.WARN}]No index found yet.[/] "
            f"[{design.MUTED}]Run a task (or launch `velune`) to build the repository index.[/]"
        )

    # --- Top knowledge areas ---
    if report.top_areas:
        areas = Text()
        for i, (area, n) in enumerate(report.top_areas):
            if i:
                areas.append("   ", style=design.FAINT)
            areas.append(f"{area} ", style=design.ACCENT_SOFT)
            areas.append(f"({n})", style=design.FAINT)
        console.print(
            Panel(
                areas,
                box=ROUNDED,
                border_style=design.FAINT,
                title=f"[{design.MUTED}]Top components[/]",
                title_align="left",
                padding=(0, 2),
            )
        )

    # --- Storage footprint ---
    store_table = Table(
        box=ROUNDED,
        border_style=design.FAINT,
        header_style=f"bold {design.ACCENT_SOFT}",
        title=f"[bold {design.ACCENT}]Persisted state[/]",
        title_justify="left",
        padding=(0, 1),
    )
    store_table.add_column("Store", style=design.INFO)
    store_table.add_column("Size", justify="right", style="default")
    store_table.add_column("Status", justify="right")
    for s in report.storage:
        status = f"[{design.OK}]present[/]" if s.exists else f"[{design.FAINT}]absent[/]"
        size = human_bytes(s.size_bytes) if s.exists else "—"
        store_table.add_row(s.name, size, status)
    console.print(store_table)

    # --- Cognitive memory tables ---
    if report.memory_tables:
        mem = Text()
        for i, t in enumerate(report.memory_tables):
            if i:
                mem.append("   ", style=design.FAINT)
            mem.append(f"{t.table} ", style=design.INFO)
            mem.append(f"{t.rows}", style="default")
        console.print(
            Panel(
                mem,
                box=ROUNDED,
                border_style=design.FAINT,
                title=f"[{design.MUTED}]Cognitive core records[/]",
                title_align="left",
                padding=(0, 2),
            )
        )

    # --- Health summary ---
    health = Text()
    for i, (state, msg) in enumerate(report.health):
        color, glyph = _STATE_GLYPH.get(state, (design.MUTED, "·"))
        if i:
            health.append("\n")
        health.append(f"{glyph} ", style=f"bold {color}")
        health.append(msg, style=design.MUTED)
    console.print(
        Panel(
            health,
            box=ROUNDED,
            border_style=design.FAINT,
            title=f"[bold {design.ACCENT}]Context health[/]",
            title_align="left",
            padding=(1, 2),
        )
    )
    console.print(f"[{design.FAINT}]Ignored: {', '.join(report.ignored_dirs)}[/]")
