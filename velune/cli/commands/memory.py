"""Memory commands — velune memory inspect/stats/clear/compact.

Phase 1 Honesty Refactor
------------------------
All three commands previously returned hardcoded fake data or performed
no actual work.  They now:

* ``inspect``  — reads real records from the episodic SQLite tier.
  Falls back gracefully to an empty result set on cold start (no DB yet).

* ``clear``    — actually calls ``delete_session()`` (episodic) or
  ``clear()`` (working) to remove data.  No longer a no-op.

* ``compact``  — emits an honest "not yet implemented" notice instead of
  printing fabricated distillation counters.  The command still succeeds
  so callers can script against it safely.
"""

from __future__ import annotations

import typer
from rich.console import Console

from velune.cli.context import CLIContext
from velune.cli.display.memory_view import MemoryDisplayView

console = Console()
memory_cmd = typer.Typer(help="Memory management commands")


@memory_cmd.command("stats")
def memory_stats(ctx: typer.Context) -> None:
    """Show visual memory map and registered policy statistics."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    config = cli_context.config

    # Compile memory statistics block
    stats = {
        "workspace": str(cli_context.workspace),
        "working_memory_ttl": config.memory.working_memory_ttl,
        "episodic_retention_days": config.memory.episodic_retention_days,
        "semantic_threshold": config.memory.semantic_threshold,
        "graph_enabled": config.memory.graph_enabled,
    }

    if cli_context.json_mode:
        import json
        print(json.dumps(stats))
        return

    display = MemoryDisplayView(console)
    display.render_memory_architecture(stats)


@memory_cmd.command("inspect")
def memory_inspect(
    ctx: typer.Context,
    tier: str = typer.Option("all", "--tier", "-t", help="Memory tier to inspect (working, episodic, all)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of records to show"),
    session_id: str = typer.Option("", "--session", "-s", help="Filter by session ID (empty = all)"),
) -> None:
    """Inspect stored records across different memory tiers.

    Reads *real* data from the episodic SQLite database.  If no data has
    been written yet (cold start), an empty table is shown rather than
    fabricated placeholder records.
    """
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    import asyncio
    asyncio.run(_memory_inspect_async(cli_context, tier, limit, session_id))


async def _memory_inspect_async(
    cli_context: CLIContext,
    tier: str,
    limit: int,
    session_id: str,
) -> None:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")

    await lifecycle.startup()

    records: list[dict] = []

    # --- Working memory (always in-process) ---
    if tier.lower() in ("working", "all"):
        try:
            working = container.get("runtime.working_memory")
            if working:
                for turn in working.get_recent_turns(limit=limit):
                    if session_id and getattr(turn, "session_id", "") != session_id:
                        continue
                    records.append({
                        "id": f"wrk-{turn.timestamp:.0f}",
                        "tier": "working",
                        "importance": 0.0,  # working tier has no importance scores yet
                        "content_preview": (turn.content[:120] + "…") if len(turn.content) > 120 else turn.content,
                        "status": turn.role,
                    })
        except Exception as exc:
            if not cli_context.json_mode:
                console.print(f"[yellow]⚠ Could not read working memory: {exc}[/yellow]")

    # --- Episodic memory (SQLite) ---
    if tier.lower() in ("episodic", "all"):
        try:
            episodic = container.get("runtime.episodic_memory")
            if episodic:
                # If no session_id filter provided, scan recent turns from a
                # heuristic default session so the table isn't always empty.
                sid = session_id or "default"
                turns = episodic.get_turns(sid)
                for turn in turns[-limit:]:
                    records.append({
                        "id": f"eps-{turn.id or turn.timestamp:.0f}",
                        "tier": "episodic",
                        "importance": 0.0,
                        "content_preview": (turn.content[:120] + "…") if len(turn.content) > 120 else turn.content,
                        "status": turn.role,
                    })
        except Exception as exc:
            if not cli_context.json_mode:
                console.print(f"[yellow]⚠ Could not read episodic memory: {exc}[/yellow]")

    if cli_context.json_mode:
        import json
        print(json.dumps({"records": records[:limit]}))
    else:
        display = MemoryDisplayView(console)
        if records:
            display.render_memory_records_table(records[:limit], tier)
        else:
            console.print(
                "[dim]No memory records found"
                + (f" for session '{session_id}'" if session_id else "")
                + ". Run a task first to populate episodic memory.[/dim]"
            )

    await lifecycle.shutdown()


@memory_cmd.command("clear")
def memory_clear(
    ctx: typer.Context,
    tier: str = typer.Argument(..., help="Memory tier to clear (working, episodic, all)"),
    session_id: str = typer.Option("default", "--session", "-s", help="Session ID to clear"),
    confirm: bool = typer.Option(False, "--confirm", "-y", help="Skip safety prompt"),
) -> None:
    """Clear memory records of a specific tier.

    Actually removes data — no longer a no-op.
    """
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not cli_context.json_mode and not confirm:
        typer.confirm(
            f"Are you sure you want to purge '{tier}' memory tier"
            + (f" for session '{session_id}'" if session_id else "")
            + "?",
            abort=True,
        )

    import asyncio
    asyncio.run(_memory_clear_async(cli_context, tier, session_id))


async def _memory_clear_async(cli_context: CLIContext, tier: str, session_id: str) -> None:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    await lifecycle.startup()

    cleared: list[str] = []
    errors: list[str] = []

    if tier.lower() in ("working", "all"):
        try:
            working = container.get("runtime.working_memory")
            if working:
                working.clear()
                cleared.append("working")
        except Exception as exc:
            errors.append(f"working: {exc}")

    if tier.lower() in ("episodic", "all"):
        try:
            episodic = container.get("runtime.episodic_memory")
            if episodic and session_id:
                episodic.delete_session(session_id)
                cleared.append(f"episodic[session={session_id}]")
        except Exception as exc:
            errors.append(f"episodic: {exc}")

    await lifecycle.shutdown()

    if cli_context.json_mode:
        import json
        print(json.dumps({"success": not errors, "cleared": cleared, "errors": errors}))
    else:
        for c in cleared:
            console.print(f"[green]✓ Cleared {c} memory.[/green]")
        for e in errors:
            console.print(f"[red]✗ Error clearing {e}[/red]")
        if not cleared and not errors:
            console.print("[dim]Nothing to clear.[/dim]")


@memory_cmd.command("compact")
def memory_compact(ctx: typer.Context) -> None:
    """Trigger the memory consolidator to compress episodic history into vectors & graph facts.

    Note: Semantic distillation (vector + graph consolidation) is not yet
    implemented in Phase 1.  This command will report honestly instead of
    printing fabricated success counters.
    """
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    import asyncio
    asyncio.run(_memory_compact_async(cli_context))


async def _memory_compact_async(cli_context: CLIContext) -> None:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")

    await lifecycle.startup()

    # Compact only what Phase 1 actually implements:
    # flush working memory turns → episodic SQLite.
    # Semantic distillation and graph consolidation are planned for a later phase.
    if not cli_context.json_mode:
        console.print("[bold cyan]⠋[/bold cyan] Flushing working memory to episodic store...")

    try:
        # shutdown() now handles the flush
        await lifecycle.shutdown()
        if not cli_context.json_mode:
            console.print("[green]✓[/green] Working memory flushed to episodic SQLite.")
            console.print(
                "[dim]Note: semantic distillation (vector + graph consolidation) "
                "is planned for a future phase and is not yet active.[/dim]"
            )
    except Exception as exc:
        await lifecycle.shutdown()
        if not cli_context.json_mode:
            console.print(f"[red]✗ Compaction error: {exc}[/red]")
        if cli_context.json_mode:
            import json
            print(json.dumps({"success": False, "error": str(exc)}))
        return

    if cli_context.json_mode:
        import json
        print(json.dumps({
            "success": True,
            "message": "Working memory flushed to episodic SQLite. Semantic compaction not yet implemented.",
        }))
    else:
        console.print("[bold green]Memory flush complete.[/bold green]")
