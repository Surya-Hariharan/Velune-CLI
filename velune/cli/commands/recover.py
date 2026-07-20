"""Crash / unsaved-session recovery — velune recover.

Each live REPL conversation is crash-guarded to a sidecar under
``~/.velune/sessions/.autosave/``. A clean exit deletes its sidecar; anything
left behind is an orphan from a crash or hard kill. This command lists those
orphans and promotes (or discards) them.
"""

from __future__ import annotations

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.table import Table

from velune.cli import design

console = Console()


def recover_cmd(
    ctx: typer.Context,
    session_id: str = typer.Argument(
        "", help="Autosave ID to recover (omit to list orphaned sessions)"
    ),
    all_: bool = typer.Option(False, "--all", "-a", help="Recover every orphaned session"),
    all_workspaces: bool = typer.Option(
        False,
        "--all-workspaces",
        help="Include orphaned sessions from every workspace, not just this one",
    ),
    discard: str = typer.Option(
        "", "--discard", help="Discard an orphaned autosave by ID without recovering"
    ),
) -> None:
    """Recover an unsaved session left behind by a crash or hard exit.

    Scoped to the current workspace by default — a crash in another project
    won't show up here unless `--all-workspaces` is passed.
    """
    from velune.cli.context import CLIContext
    from velune.cli.sessions import SessionStore

    store = SessionStore()
    cli_context = ctx.obj
    workspace = (
        None
        if all_workspaces or not isinstance(cli_context, CLIContext)
        else str(cli_context.workspace.resolve())
    )

    if discard:
        if store.discard_autosave(discard):
            console.print(f"[{design.OK}]Discarded autosave [bold]{discard}[/bold].[/{design.OK}]")
        else:
            console.print(
                f"[{design.DANGER}]No orphaned autosave '{discard}' found.[/{design.DANGER}]"
            )
            raise typer.Exit(1)
        return

    orphans = store.list_orphaned_autosaves(workspace=workspace)

    if all_:
        if not orphans:
            console.print(f"[{design.MUTED}]No unsaved sessions to recover.[/{design.MUTED}]")
            return
        for meta in orphans:
            saved = store.recover_autosave(meta.id)
            if saved:
                console.print(f"[{design.OK}]Recovered[/{design.OK}] {saved.id} — {saved.title}")
        console.print(f"[{design.MUTED}]Recovered {len(orphans)} session(s).[/{design.MUTED}]")
        return

    if session_id:
        saved = store.recover_autosave(session_id)
        if saved is None:
            console.print(
                f"[{design.DANGER}]No orphaned autosave '{session_id}' found.[/{design.DANGER}]"
            )
            raise typer.Exit(1)
        console.print(
            f"[{design.OK}]Recovered session [bold]{saved.id}[/bold] — {saved.title}[/{design.OK}]"
        )
        console.print(
            f"[{design.MUTED}]Resume it:[/] [bold]velune chat --session {saved.id}[/bold]"
        )
        return

    # No id, no flags → list orphaned autosaves.
    if not orphans:
        console.print(f"[{design.OK}]No unsaved sessions — nothing to recover.[/{design.OK}]")
        return

    table = Table(box=ROUNDED, border_style=design.FAINT, expand=False)
    table.add_column("ID", style=design.ACCENT, no_wrap=True)
    table.add_column("Title", style=design.INFO)
    table.add_column("Project", style=design.MUTED)
    table.add_column("Updated", style=design.MUTED, no_wrap=True)
    table.add_column("Turns", justify="right", style=design.MUTED)

    for m in orphans:
        table.add_row(
            m.id,
            m.title,
            m.project_name,
            m.updated_at[:16].replace("T", " "),
            str(m.turn_count),
        )

    console.print(table)
    console.print()
    if workspace:
        console.print(
            f"[{design.MUTED}]Showing this workspace only — "
            f"[/][bold]velune recover --all-workspaces[/bold][{design.MUTED}] for every project.[/]"
        )
    console.print(
        f"[{design.MUTED}]Recover one:[/] [bold]velune recover <id>[/bold]   "
        f"[{design.MUTED}]all:[/] [bold]velune recover --all[/bold]"
    )
