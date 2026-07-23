"""Session management slash command handlers: /session /new (archive)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.session_mgmt")


async def cmd_session(repl: VeluneREPL, args: str) -> None:
    from pathlib import Path as _Path

    from velune.cli.session_manager import export_session_markdown, save_session

    workspace = str(repl.container.get("runtime.workspace") or "")
    model_id = repl.active_model.model_id if repl.active_model else "unknown"
    parts = args.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    sub_args = parts[1] if len(parts) > 1 else ""

    if not sub:
        await _session_picker(repl, workspace)

    elif sub == "save":
        session_id = save_session(repl._conversation, model_id, workspace)
        repl.console.print(f"[green]Session saved:[/green] [cyan]{session_id}[/cyan]")

    elif sub == "list":
        await _cmd_session_list(repl, workspace)

    elif sub == "resume":
        if not sub_args:
            repl.console.print("[yellow]Usage: /session resume <id>[/yellow]")
            return
        await _cmd_session_resume(repl, sub_args.strip())

    elif sub == "summary":
        if not sub_args:
            repl.console.print("[yellow]Usage: /session summary <id>[/yellow]")
            return
        await _cmd_session_summary(repl, sub_args.strip())

    elif sub == "export":
        target = sub_args.strip()
        if not target:
            target = save_session(repl._conversation, model_id, workspace)
        md = export_session_markdown(target)
        if md is None:
            repl.console.print(f"[red]Session '{target}' not found.[/red]")
            return
        out_path = _Path.cwd() / f"velune-session-{target}.md"
        out_path.write_text(md, encoding="utf-8")
        repl.console.print(f"[green]Exported to:[/green] {out_path}")

    else:
        repl.console.print(
            f"[red]Unknown subcommand: {sub!r}[/red]  "
            "[dim]Use list | resume <id> | summary <id> | save | export[/dim]"
        )


async def _session_picker(repl: VeluneREPL, workspace: str) -> None:
    """Interactive session picker: archived snapshots, resumable on Enter."""
    from pathlib import Path

    from velune.cli.picker import PickItem, pick

    metas = repl._session_store.list(limit=50)
    if not metas:
        repl.console.print(
            "[dim]No saved sessions yet. /new archives the current "
            "conversation; /session save snapshots it explicitly.[/dim]"
        )
        return

    def _is_current_ws(m) -> bool:
        try:
            return Path(m.workspace).resolve() == Path(workspace).resolve()
        except Exception:
            return m.workspace == workspace

    metas.sort(key=lambda m: (not _is_current_ws(m), m.project_name))
    items = [
        PickItem(
            id=m.id,
            label=m.title,
            meta=f"{m.updated_at[:16].replace('T', ' ')} · {m.model_id} · {m.turn_count} turns",
            group=m.project_name,
        )
        for m in metas
    ]
    chosen = await pick("Resume a session", items)
    if chosen is None:
        return
    await _resume_snapshot(repl, chosen.id)


async def _resume_snapshot(repl: VeluneREPL, session_id: str) -> bool:
    """Load an archived snapshot into the live conversation context."""
    loaded = repl._session_store.load(session_id)
    if loaded is None:
        return False
    meta, conversation = loaded
    # Archive the live conversation before replacing it so in-progress turns
    # are never silently discarded, then adopt the resumed session's id so
    # exit writes back to that slot instead of the REPL's original random id.
    repl._archive_current_session()
    await repl._end_episodic_session()
    repl._conversation = conversation
    repl._session_id = session_id
    repl.session_tokens = meta.total_tokens
    await repl._start_episodic_session()
    repl.console.print(
        f"[green]Resumed[/green] [cyan]{meta.title}[/cyan] "
        f"[dim]({meta.turn_count} turns · {meta.model_id})[/dim]"
    )
    return True


async def _cmd_session_list(repl: VeluneREPL, workspace: str) -> None:
    """List sessions from the canonical JSON `SessionStore` — the same store
    `velune session list` (the top-level CLI command) reads. Previously this
    queried the separate SQLite episodic tier instead, which mints its own
    independent session id — the two could show different, unsynced session
    lists depending on whether you asked from the shell or the REPL. Now both
    surfaces read the one source of truth.
    """
    try:
        sessions = repl._session_store.list(workspace=workspace, limit=10)
    except Exception as exc:
        from velune.cli.ui_components import print_notification

        print_notification(repl.console, f"Could not load sessions: {exc}", type="error")
        return

    from velune.cli.ui_components import create_table, print_header, print_notification

    if not sessions:
        print_notification(repl.console, "No sessions found for this workspace.", type="info")
        return

    table = create_table("ID", "Started", "Model", "Tokens", "Title")

    for m in sessions:
        dt = m.created_at[:16].replace("T", " ") if m.created_at else "—"
        table.add_row(m.id, dt, m.model_id or "—", str(m.total_tokens), m.title)

    print_header(repl.console, "Recent Sessions")
    repl.console.print(table)
    repl.console.print()


async def _cmd_session_resume(repl: VeluneREPL, session_id: str) -> None:
    try:
        if await _resume_snapshot(repl, session_id):
            return
    except Exception:
        pass
    try:
        episodic = repl.container.get("runtime.episodic_session_memory")
        turns = await episodic.get_recent_turns(session_id, limit=20)
    except Exception as exc:
        repl.console.print(f"[red]Could not load session: {exc}[/red]")
        return

    if not turns:
        repl.console.print(f"[red]Session '{session_id}' not found or has no turns.[/red]")
        return

    # Same data-loss guard as the snapshot path: preserve the live conversation
    # before overwriting it with the episodic reconstruction.
    repl._archive_current_session()
    repl._conversation = [{"role": t.role, "content": t.content} for t in turns]
    repl.console.print(
        f"[green]Resumed[/green] [cyan]{session_id}[/cyan] "
        f"[dim]({len(repl._conversation)} turns loaded into context)[/dim]"
    )


async def _cmd_session_summary(repl: VeluneREPL, session_id: str) -> None:
    from rich.panel import Panel

    try:
        episodic = repl.container.get("runtime.episodic_session_memory")
    except Exception as exc:
        repl.console.print(f"[red]Could not access episodic memory: {exc}[/red]")
        return

    existing = await episodic.get_session_summary(session_id)
    if existing:
        repl.console.print(
            Panel(
                existing,
                title=f"[bold cyan]Session Summary — {session_id}[/bold cyan]",
                border_style="cyan",
            )
        )
        return

    turns = await episodic.get_session_history(session_id)
    if not turns:
        repl.console.print(f"[yellow]No turns found for session '{session_id}'.[/yellow]")
        return

    model, provider = await repl._resolve_active_model_and_provider()
    if not model or not provider:
        repl.console.print("[yellow]No model available to generate summary.[/yellow]")
        return

    turn_text = "\n".join(f"{t.role.upper()}: {t.content[:300]}" for t in turns[:20])
    from velune.core.types.inference import InferenceRequest

    req = InferenceRequest(
        model_id=model.model_id,
        messages=[
            {
                "role": "user",
                "content": (
                    "Summarize this conversation in 2–3 sentences, "
                    "focusing on what was accomplished:\n\n" + turn_text
                ),
            }
        ],
        temperature=0.3,
        max_tokens=256,
    )
    with repl.console.status("[cyan]Generating summary...[/cyan]"):
        response = await provider.infer(req)
    summary_text = response.content.strip()
    await episodic.set_session_summary(session_id, summary_text)
    repl.console.print(
        Panel(
            summary_text,
            title=f"[bold cyan]Session Summary — {session_id}[/bold cyan]",
            border_style="cyan",
        )
    )
