"""Session management commands — velune session list / delete / export."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.table import Table

from velune.cli import design
from velune.cli.context import CLIContext

console = Console()
session_cmd = typer.Typer(help="List, resume, or delete chat sessions.")


@session_cmd.command("list")
def session_list(
    ctx: typer.Context,
    all_workspaces: bool = typer.Option(
        False, "--all", "-a", help="Show sessions from all workspaces"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum sessions to display"),
) -> None:
    """List saved chat sessions for the current workspace."""
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    workspace = None if all_workspaces else str(cli_context.workspace.resolve())
    sessions = store.list(workspace=workspace, limit=limit)

    if cli_context.json_mode:
        import json

        print(
            json.dumps(
                [
                    {
                        "id": m.id,
                        "title": m.title,
                        "project": m.project_name,
                        "updated_at": m.updated_at,
                        "turns": m.turn_count,
                    }
                    for m in sessions
                ]
            )
        )
        return

    if not sessions:
        label = "any workspace" if all_workspaces else str(cli_context.workspace)
        console.print(f"[{design.MUTED}]No sessions found for {label}.[/]")
        console.print(f"[{design.MUTED}]Start one with:[/] [bold]velune chat[/bold]")
        return

    table = Table(box=ROUNDED, border_style=design.FAINT, expand=False)
    table.add_column("ID", style=design.ACCENT, no_wrap=True)
    table.add_column("Title", style=design.INFO)
    table.add_column("Project", style=design.MUTED)
    table.add_column("Updated", style=design.MUTED, no_wrap=True)
    table.add_column("Turns", justify="right", style=design.MUTED)

    for m in sessions:
        table.add_row(
            m.id,
            m.title,
            m.project_name,
            m.updated_at[:16].replace("T", " "),
            str(m.turn_count),
        )

    console.print(table)
    console.print()
    console.print(f"[{design.MUTED}]Resume a session:[/] [bold]velune chat --session <id>[/bold]")
    console.print(f"[{design.MUTED}]Delete a session: [/] [bold]velune session delete <id>[/bold]")


@session_cmd.command("delete")
def session_delete(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a saved session."""
    from velune.cli.sessions import SessionStore

    store = SessionStore()
    meta = store.load_meta(session_id)
    if meta is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)

    if not yes:
        typer.confirm(f"Delete session '{meta.title}' ({session_id})?", default=False, abort=True)

    store.delete(session_id)
    console.print(f"[{design.OK}]Session [bold]{session_id}[/bold] deleted.[/{design.OK}]")


@session_cmd.command("export")
def session_export(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to export"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output file path (default: stdout)"
    ),
) -> None:
    """Export a session as Markdown."""
    from velune.cli.sessions import SessionStore

    store = SessionStore()
    md = store.export_markdown(session_id)
    if md is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)

    if output:
        output.write_text(md, encoding="utf-8")
        console.print(f"[{design.OK}]Exported to {output}[/{design.OK}]")
    else:
        console.print(md)
