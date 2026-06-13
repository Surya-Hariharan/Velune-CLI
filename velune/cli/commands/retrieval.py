"""``velune retrieval trace`` — prove the retrieval pipeline is real.

Runs a genuine query through the live hybrid retriever and shows exactly which
sub-retrievers fired (lexical / vector / graph), how long each took, how many
candidates they produced, and the final ranked, redacted hits. Every figure is
measured during the run — see
:func:`velune.observability.retrieval_report.build_retrieval_trace`.

    velune retrieval trace "where is the sandbox validated?"
    velune retrieval trace "auth" --top-k 5 --no-vector
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from velune.cli import design
from velune.cli.context import CLIContext

if TYPE_CHECKING:
    from velune.observability.retrieval_report import RetrievalTraceReport

console = Console()
retrieval_cmd = typer.Typer(help="Inspect and trace the retrieval pipeline")

_SOURCE_STYLE = {
    "vector": design.ACCENT_SOFT,
    "lexical": design.OK,
    "graph": design.INFO,
    "memory": design.HIGHLIGHT,
}


@retrieval_cmd.command("trace")
def retrieval_trace(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="The query to run through the retriever"),
    top_k: int = typer.Option(10, "--top-k", "-k", min=1, max=100, help="Max hits to return"),
    namespace: str = typer.Option(None, "--namespace", "-n", help="Restrict to a namespace"),
    use_vector: bool = typer.Option(True, "--vector/--no-vector", help="Enable vector search"),
    use_graph: bool = typer.Option(True, "--graph/--no-graph", help="Enable graph traversal"),
) -> None:
    """Trace a real retrieval run: stage timings, candidate counts, ranked hits."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    from velune.core.event_loop import submit

    submit(_retrieval_trace_async(cli_context, query, top_k, namespace, use_vector, use_graph))


async def _retrieval_trace_async(
    cli_context: CLIContext,
    query: str,
    top_k: int,
    namespace: str | None,
    use_vector: bool,
    use_graph: bool,
) -> None:
    from velune.observability.retrieval_report import build_retrieval_trace
    from velune.retrieval.schemas import RetrievalQuery

    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")

    await lifecycle.startup()
    try:
        retriever = container.get("runtime.retrieval")

        retrieval_query = RetrievalQuery(
            text=query,
            top_k=top_k,
            namespace=namespace,
            vector_weight=0.5 if use_vector else 0.0,
            lexical_weight=0.3,
            graph_weight=0.2 if use_graph else 0.0,
        )

        if not cli_context.json_mode:
            with console.status("[bold cyan]Running retrieval pipeline...[/bold cyan]"):
                result = await retriever.retrieve(retrieval_query)
        else:
            result = await retriever.retrieve(retrieval_query)

        report = build_retrieval_trace(result)

        if cli_context.json_mode:
            print(json.dumps(report.to_dict()))
        else:
            _render(console, report)
    finally:
        await lifecycle.shutdown()


def _render(console: Console, report: RetrievalTraceReport) -> None:
    """Render the retrieval trace as calm, infrastructure-grade panels."""
    header = Text()
    header.append("Query     ", style=design.MUTED)
    header.append(f"{report.query}\n", style=f"bold {design.ACCENT}")
    header.append("Strategy  ", style=design.MUTED)
    header.append(f"{report.strategy}", style=design.INFO)
    header.append("   top-k ", style=design.MUTED)
    header.append(f"{report.top_k}", style="bold")
    header.append("   total ", style=design.MUTED)
    header.append(f"{report.total_ms:.1f} ms", style="bold")
    console.print(
        Panel(
            header,
            border_style=design.ACCENT_SOFT,
            box=ROUNDED,
            title="[bold]Retrieval Trace[/bold]",
        )
    )

    # --- Stage table ---
    table = Table(box=ROUNDED, border_style=design.FAINT, title="[bold]Pipeline Stages[/bold]")
    table.add_column("Stage", style=design.INFO)
    table.add_column("State", justify="center")
    table.add_column("Hits", justify="right", style=design.MUTED)
    table.add_column("Time", justify="right", style=design.MUTED)
    table.add_column("Note", style=design.MUTED)
    for s in report.stages:
        if not s.enabled:
            state = Text("○ off", style=design.MUTED)
        else:
            state = Text("● on", style=design.OK)
        table.add_row(s.name, state, str(s.hits), f"{s.ms:.1f} ms", s.note or "")
    console.print()
    console.print(table)

    # --- Hits ---
    console.print()
    if report.hits:
        hits = Table(box=ROUNDED, border_style=design.FAINT, title="[bold]Ranked Hits[/bold]")
        hits.add_column("#", justify="right", style=design.MUTED)
        hits.add_column("Score", justify="right")
        hits.add_column("Source")
        hits.add_column("Document", style=design.INFO)
        hits.add_column("Snippet", style=design.MUTED)
        for h in report.hits:
            src_style = _SOURCE_STYLE.get(h.source, design.MUTED)
            hits.add_row(
                str(h.rank),
                f"{h.score:.3f}",
                Text(h.source, style=src_style),
                h.label,
                h.snippet,
            )
        console.print(hits)
    else:
        console.print(f"[{design.MUTED}]No hits returned.[/]")

    for note in report.notes:
        console.print(f"[{design.WARN}]›[/] [{design.MUTED}]{note}[/]")
