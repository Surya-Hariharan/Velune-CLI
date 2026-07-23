"""Memory, context, and knowledge graph slash command handlers: /memory /context /graph."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.memory")


async def cmd_memory(repl: VeluneREPL, args: str) -> None:
    from velune.cli.display.memory_view import MemoryDisplayView

    sub = args.strip().lower()
    working = repl.container.get("runtime.working_memory")

    if sub == "clear":
        from velune.cli.handlers.confirm import confirm_destructive

        if not await confirm_destructive(repl, "Clear working memory? This cannot be undone."):
            repl.console.print("[dim]Cancelled.[/dim]")
            return
        working.clear()
        repl.console.print("[green]Working memory cleared.[/green]")
        return

    # MemoryLifecycleManager.health() — the same call `velune memory stats`
    # and `velune doctor` use, so all three surfaces report the same numbers
    # instead of each hand-rolling its own (previously disagreeing) view.
    # This used to hardcode "Qdrant local" for the semantic tier and "—" for
    # tiers 3-5 regardless of what was actually indexed.
    try:
        manager = repl.container.get("runtime.memory_lifecycle")
    except Exception:
        manager = None

    if manager is not None:
        try:
            health = await manager.health()
            MemoryDisplayView(repl.console).render_memory_health(health)
        except Exception as exc:
            repl.console.print(f"[yellow]Could not read memory health: {exc}[/yellow]")
    else:
        repl.console.print("[yellow]Memory lifecycle manager is not available.[/yellow]")

    recent = working.get_recent_turns(3)
    if recent:
        repl.console.print("\n[dim]Recent working memory turns:[/dim]")
        for t in recent:
            preview = t.content[:80].replace("\n", " ")
            repl.console.print(f"  [dim]{t.role}:[/dim] {preview}…")
    repl.console.print(
        "[dim]→ /graph to explore the knowledge graph  ·  /context to see token usage[/dim]"
    )


async def cmd_context(repl: VeluneREPL, args: str) -> None:
    from velune.context.token_counter import estimate_tokens

    if not repl._conversation:
        repl.console.print("[dim]No conversation context yet.[/dim]")
        return

    used = estimate_tokens(" ".join(m["content"] for m in repl._conversation))
    limit = repl.active_model.context_length if repl.active_model else 8192
    pct = (used / limit) * 100 if limit > 0 else 0.0
    turns = len(repl._conversation)
    repl.console.print(
        f"[cyan]Context:[/cyan] {used:,} / {limit:,} tokens "
        f"[dim]({pct:.1f}% used · {turns} turns)[/dim]"
    )
    if pct > 85:
        repl.console.print(
            "[yellow]Context window nearly full. Type /clear to reset conversation.[/yellow]"
        )


async def cmd_graph(repl: VeluneREPL, args: str) -> None:
    """Render a hierarchical tree of knowledge graph entities."""
    graph_memory = repl.container.get("runtime.graph_memory")
    if not graph_memory:
        repl.console.print("[red]Graph memory tier is not initialized.[/red]")
        return

    entities = await graph_memory.get_all_nodes()
    relations = await graph_memory.get_all_edges()

    entities_dicts = [
        {
            "id": n.id,
            "type": n.node_type,
            "importance": n.properties.get("importance", 1.0),
            "name": n.properties.get("name", n.id),
        }
        for n in entities
    ]
    relations_dicts = [
        {
            "source": r.source,
            "target": r.target,
            "relation": r.relation_type,
        }
        for r in relations
    ]

    from velune.cli.display.memory_view import MemoryDisplayView

    view = MemoryDisplayView(repl.console)
    view.render_knowledge_graph(entities_dicts, relations_dicts)
