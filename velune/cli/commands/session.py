"""Session management commands — velune session list / delete / export."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
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
    archived: bool = typer.Option(
        False, "--archived", help="Show only archived sessions instead of active ones"
    ),
) -> None:
    """List saved chat sessions for the current workspace."""
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    workspace = None if all_workspaces else str(cli_context.workspace.resolve())
    sessions = store.list(workspace=workspace, limit=limit, archived_only=archived)

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
                        "archived": m.archived,
                    }
                    for m in sessions
                ]
            )
        )
        return

    if not sessions:
        label = "any workspace" if all_workspaces else str(cli_context.workspace)
        if archived:
            console.print(f"[{design.MUTED}]No archived sessions for {label}.[/]")
        else:
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
    if archived:
        console.print(
            f"[{design.MUTED}]Restore a session:[/] [bold]velune session unarchive <id>[/bold]"
        )
    else:
        console.print(
            f"[{design.MUTED}]Resume a session:[/] [bold]velune session resume <id>[/bold]"
        )
        console.print(
            f"[{design.MUTED}]Archive a session:[/] [bold]velune session archive <id>[/bold]"
        )
    console.print(f"[{design.MUTED}]Delete a session: [/] [bold]velune session delete <id>[/bold]")


@session_cmd.command("delete")
def session_delete(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    any_workspace: bool = typer.Option(
        False, "--any-workspace", help="Allow acting on a session from a different workspace"
    ),
) -> None:
    """Delete a saved session."""
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    meta = store.load_meta(session_id)
    if meta is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)
    if not _check_same_workspace(
        store, cli_context, meta, any_workspace=any_workspace, verb="Delete"
    ):
        raise typer.Exit(1)

    if not yes:
        typer.confirm(f"Delete session '{meta.title}' ({session_id})?", default=False, abort=True)

    store.delete(session_id)
    console.print(f"[{design.OK}]Session [bold]{session_id}[/bold] deleted.[/{design.OK}]")


def _resolve_session_id(store, cli_context: CLIContext, session_id: str | None) -> str | None:
    """Resolve an explicit id, or ``last``/omitted to the newest session here."""
    if session_id and session_id.lower() != "last":
        return session_id
    workspace = str(cli_context.workspace.resolve())
    recent = store.list(workspace=workspace, limit=1, include_archived=True)
    return recent[0].id if recent else None


def _check_same_workspace(
    store, cli_context: CLIContext, meta, *, any_workspace: bool, verb: str
) -> bool:
    """Refuse to act on another workspace's session unless explicitly asked.

    A session id from `velune session list --all` (or copy-pasted from
    another project) would otherwise let a delete/show/archive/export target
    a session that isn't this project's without any warning — since ids are
    just short hex strings, that's an easy accidental cross-workspace action.
    Prints an actionable error and returns False when blocked.
    """
    if any_workspace:
        return True
    from velune.cli.sessions import SessionStore

    if SessionStore._same_workspace(meta.workspace, str(cli_context.workspace.resolve())):
        return True
    console.print(
        f"[{design.DANGER}]Session '{meta.id}' belongs to a different workspace[/] "
        f"([{design.MUTED}]{meta.project_name}[/]).\n"
        f"[{design.MUTED}]{verb} it anyway with:[/] [bold]--any-workspace[/bold]"
    )
    return False


@session_cmd.command("resume")
def session_resume(
    ctx: typer.Context,
    session_id: str = typer.Argument(
        None, help="Session ID to resume. Omit (or 'last') to resume the most recent."
    ),
) -> None:
    """Resume a saved chat session (the newest one if no ID is given)."""
    from velune.cli.commands.chat import chat_command
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    resolved = _resolve_session_id(store, cli_context, session_id)
    if resolved is None:
        console.print(f"[{design.MUTED}]No sessions to resume in this workspace.[/]")
        console.print(f"[{design.MUTED}]Start one with:[/] [bold]velune chat[/bold]")
        raise typer.Exit(1)

    if store.load_meta(resolved) is None:
        console.print(f"[{design.DANGER}]Session '{resolved}' not found.[/]")
        raise typer.Exit(1)

    # Resuming an archived session brings it back into the active list.
    store.set_archived(resolved, False)
    chat_command(ctx, session_id=resolved)


@session_cmd.command("show")
def session_show(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to inspect"),
    turns: int = typer.Option(6, "--turns", "-n", help="How many recent turns to preview"),
    any_workspace: bool = typer.Option(
        False, "--any-workspace", help="Allow acting on a session from a different workspace"
    ),
) -> None:
    """Show a session's metadata and a preview of its most recent turns."""
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    loaded = store.load(session_id)
    if loaded is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)
    meta, conversation = loaded
    if not _check_same_workspace(
        store, cli_context, meta, any_workspace=any_workspace, verb="Show"
    ):
        raise typer.Exit(1)

    if cli_context.json_mode:
        import json

        print(
            json.dumps(
                {
                    "id": meta.id,
                    "title": meta.title,
                    "project": meta.project_name,
                    "workspace": meta.workspace,
                    "model_id": meta.model_id,
                    "mode": meta.mode,
                    "created_at": meta.created_at,
                    "updated_at": meta.updated_at,
                    "turns": meta.turn_count,
                    "total_tokens": meta.total_tokens,
                    "archived": meta.archived,
                    "conversation": conversation,
                }
            )
        )
        return

    header = (
        f"[bold]{meta.title}[/bold]  [{design.MUTED}]{meta.id}[/]\n"
        f"[{design.MUTED}]Project:[/] {meta.project_name}   "
        f"[{design.MUTED}]Model:[/] {meta.model_id}   "
        f"[{design.MUTED}]Mode:[/] {meta.mode}\n"
        f"[{design.MUTED}]Updated:[/] {meta.updated_at.replace('T', ' ')}   "
        f"[{design.MUTED}]Turns:[/] {meta.turn_count}   "
        f"[{design.MUTED}]Tokens:[/] {meta.total_tokens:,}"
    )
    if meta.archived:
        header += f"   [{design.WARN}](archived)[/]"
    console.print(Panel(header, box=ROUNDED, border_style=design.FAINT))

    recent = conversation[-turns:] if turns > 0 else conversation
    for turn in recent:
        role = turn.get("role", "unknown")
        style = design.ACCENT if role == "user" else design.INFO
        content = (turn.get("content", "") or "").strip()
        if len(content) > 500:
            content = content[:500].rstrip() + " …"
        console.print(f"[bold {style}]{role.capitalize()}[/]")
        console.print(content or f"[{design.MUTED}](empty)[/]")
        console.print()

    console.print(f"[{design.MUTED}]Resume:[/] [bold]velune session resume {meta.id}[/bold]")


@session_cmd.command("archive")
def session_archive(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to archive"),
    any_workspace: bool = typer.Option(
        False, "--any-workspace", help="Allow acting on a session from a different workspace"
    ),
) -> None:
    """Archive a session — hide it from the default list without deleting it.

    Archiving is non-destructive: the conversation is untouched and project
    memory/embeddings are unaffected. Reverse it with `velune session unarchive`.
    """
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    meta = store.load_meta(session_id)
    if meta is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)
    if not _check_same_workspace(
        store, cli_context, meta, any_workspace=any_workspace, verb="Archive"
    ):
        raise typer.Exit(1)
    if meta.archived:
        console.print(f"[{design.MUTED}]Session '{session_id}' is already archived.[/]")
        return
    store.set_archived(session_id, True)
    console.print(
        f"[{design.OK}]Archived[/{design.OK}] [bold]{session_id}[/bold] "
        f"[{design.MUTED}]— restore with `velune session unarchive {session_id}`.[/]"
    )


@session_cmd.command("unarchive")
def session_unarchive(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to restore from the archive"),
    any_workspace: bool = typer.Option(
        False, "--any-workspace", help="Allow acting on a session from a different workspace"
    ),
) -> None:
    """Restore an archived session back into the active list."""
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    meta = store.load_meta(session_id)
    if meta is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)
    if not _check_same_workspace(
        store, cli_context, meta, any_workspace=any_workspace, verb="Unarchive"
    ):
        raise typer.Exit(1)
    if not meta.archived:
        console.print(f"[{design.MUTED}]Session '{session_id}' is not archived.[/]")
        return
    store.set_archived(session_id, False)
    console.print(
        f"[{design.OK}]Restored[/{design.OK}] [bold]{session_id}[/bold] to the active list."
    )


@session_cmd.command("export")
def session_export(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session ID to export"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output file path (default: stdout)"
    ),
    any_workspace: bool = typer.Option(
        False, "--any-workspace", help="Allow acting on a session from a different workspace"
    ),
) -> None:
    """Export a session as Markdown."""
    from velune.cli.sessions import SessionStore

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    store = SessionStore()
    meta = store.load_meta(session_id)
    if meta is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)
    if not _check_same_workspace(
        store, cli_context, meta, any_workspace=any_workspace, verb="Export"
    ):
        raise typer.Exit(1)

    md = store.export_markdown(session_id)
    if md is None:
        console.print(f"[{design.DANGER}]Session '{session_id}' not found.[/]")
        raise typer.Exit(1)

    if output:
        output.write_text(md, encoding="utf-8")
        console.print(f"[{design.OK}]Exported to {output}[/{design.OK}]")
    else:
        console.print(md)
