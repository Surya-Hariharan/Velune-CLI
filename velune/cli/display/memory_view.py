"""Rich terminal rendering of hierarchical memory tiers, priority decays, and graph entities."""

from __future__ import annotations

from typing import Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree


class MemoryDisplayView:
    """Beautiful Rich-based UI components to visualize Velune's 5-tier Hierarchical Memory system."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def render_memory_architecture(self, stats: dict[str, Any]) -> None:
        """Render a magnificent visual map of the memory tiers and active index statistics."""
        self.console.print(
            Panel(
                Text.assemble(
                    ("[bold magenta]VELUNE CORE HIERARCHICAL MEMORY MAP[/bold magenta]\n"),
                    (
                        "[dim]Active Workspace:[/dim] [italic cyan]"
                        + str(stats.get("workspace", "current"))
                        + "[/italic cyan]\n\n"
                    ),
                    (
                        "[bold yellow]Tier 1: Working Memory[/bold yellow]  ──► In-memory state, fast lookups (TTL: "
                        + str(stats.get("working_memory_ttl", 3600))
                        + "s)\n"
                    ),
                    (
                        "[bold green]Tier 2: Episodic SQLite[/bold green] ──► Task runs, step histories (Retention: "
                        + str(stats.get("episodic_retention_days", 30))
                        + " days)\n"
                    ),
                    (
                        "[bold blue]Tier 3: Semantic LanceDB[/bold blue]  ──► Vector code snippet indices (Similarity Threshold: "
                        + str(stats.get("semantic_threshold", 0.85))
                        + ")\n"
                    ),
                    (
                        "[bold cyan]Tier 4: Graphiti Graph[/bold cyan]   ──► Entity relationships & AST Dependency Graph (Graphiti Enabled: "
                        + str(stats.get("graph_enabled", True))
                        + ")\n"
                    ),
                    (
                        "[bold red]Tier 5: Archive Storage[/bold red]  ──► Long-term zstd-compressed cold files"
                    ),
                ),
                title="[bold white]Memory Architecture Map[/bold white]",
                border_style="magenta",
                box=ROUNDED,
                title_align="left",
            )
        )

    def render_memory_health(self, health: Any) -> None:
        """Render live ``MemoryHealth`` metrics — the one table every memory-
        health surface (``velune memory stats``, the REPL's ``/memory``, and
        ``velune doctor``'s one-line summary) now derives its numbers from,
        instead of each hand-rolling its own (previously disagreeing) view.

        Tier 3 is LanceDB, not Qdrant — the semantic tier migrated away from
        Qdrant a while back, and the older label here (and in the REPL's
        ``/memory``) was never updated, quietly describing a store this
        system no longer uses.
        """
        table = Table(
            title="[bold green]Memory Health[/bold green]",
            box=ROUNDED,
            border_style="green",
            expand=True,
        )
        table.add_column("Tier", style="cyan")
        table.add_column("Status", style="dim")
        table.add_column("Count / Size", style="white", justify="right")
        table.add_column("Notes", style="dim")

        table.add_row(
            "Tier 1 · Working",
            "[green]active[/green]",
            str(health.working_memory_turns),
            "in-process turn buffer",
        )
        table.add_row(
            "Tier 2 · Episodic",
            "[green]active[/green]" if health.episodic_sessions else "[dim]empty[/dim]",
            str(health.episodic_sessions),
            "SQLite persisted",
        )
        table.add_row(
            "Tier 3 · Semantic",
            "[green]active[/green]" if health.semantic_indexed_count else "[dim]empty[/dim]",
            str(health.semantic_indexed_count),
            f"LanceDB · {health.lancedb_size_mb:.1f} MB",
        )
        table.add_row(
            "Embedding queue",
            "[yellow]backlogged[/yellow]" if health.embedding_queue_depth else "[green]clear[/green]",
            str(health.embedding_queue_depth),
            "pending semantic-index writes",
        )
        self.console.print(table)
        self.console.print()

    def render_memory_records_table(self, records: list[dict[str, Any]], memory_type: str) -> None:
        """Render a structured table showing registered records across specific memory tiers."""
        table = Table(
            title=f"[bold green]Registered Memory Records ({memory_type.capitalize()})[/bold green]",
            box=ROUNDED,
            border_style="green",
            expand=True,
        )
        table.add_column("Record ID / Key", style="bold cyan", width=25)
        table.add_column("Memory Tier", style="magenta")
        table.add_column("Importance Score", style="yellow")
        table.add_column("Content Preview", style="white")
        table.add_column("Age (s) / Status", style="blue")

        for rec in records:
            importance = rec.get("importance", 1.0)
            importance_bar = "★" * int(importance * 5)
            table.add_row(
                rec.get("id", "N/A"),
                rec.get("tier", memory_type),
                f"{importance:.2f} ({importance_bar})",
                rec.get("content_preview", ""),
                rec.get("status", "Active"),
            )
        self.console.print(table)
        self.console.print()

    def render_knowledge_graph(
        self, entities: list[dict[str, Any]], relations: list[dict[str, Any]]
    ) -> None:
        """Render a beautiful hierarchical tree of knowledge graph entities and their relational links."""
        root = Tree("[bold cyan]Graphiti Knowledge Graph Root[/bold cyan]")

        # Index entities by type for rendering
        by_type: dict[str, list[dict[str, Any]]] = {}
        for ent in entities:
            etype = ent.get("type", "entity").upper()
            if etype not in by_type:
                by_type[etype] = []
            by_type[etype].append(ent)

        for etype, items in by_type.items():
            type_node = root.add(f"[bold yellow]{etype}[/bold yellow]")
            for item in items:
                name = item.get("name", item.get("id", "Unknown"))
                importance = item.get("importance", 1.0)
                item_node = type_node.add(f"[cyan]{name}[/cyan] [dim](imp: {importance:.2f})[/dim]")

                # Find relations where this item is the source
                for rel in relations:
                    if rel.get("source") == item.get("id"):
                        item_node.add(
                            f"──[magenta]{rel.get('relation', 'connected')}[/magenta]──► [white]{rel.get('target')}[/white]"
                        )

        self.console.print(
            Panel(
                root,
                title="[bold white]Knowledge Graph Visualization[/bold white]",
                border_style="cyan",
                box=ROUNDED,
            )
        )
        self.console.print()
