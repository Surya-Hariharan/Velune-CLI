"""Workspace commands — velune workspace init/status/graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from velune.cli import design
from velune.cli.context import CLIContext

if TYPE_CHECKING:
    from velune.observability.workspace_graph import (
        DependencyGraphReport,
        FocusView,
        GraphNodeStat,
    )

console = Console()
workspace_cmd = typer.Typer(help="Workspace management commands")


@workspace_cmd.command("init")
def workspace_init(
    ctx: typer.Context,
    path: Path = typer.Argument(Path.cwd(), help="Workspace path"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reinitialization and re-index"),
) -> None:
    """Initialize a Velune workspace and build initial Tree-sitter AST parser indices."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not cli_context.json_mode:
        console.print(f"[bold cyan]Initializing Velune Cognitive Workspace at:[/bold cyan] {path}")

    # 1. Create .velune directory structure
    velune_dir = path / ".velune"
    velune_dir.mkdir(exist_ok=True)
    (velune_dir / "memory").mkdir(exist_ok=True)
    (velune_dir / "retrieval").mkdir(exist_ok=True)
    (velune_dir / "index").mkdir(exist_ok=True)
    (velune_dir / "snapshots").mkdir(exist_ok=True)

    if not cli_context.json_mode:
        console.print("[green]✓[/green] Created .velune configuration directory structure.")

    # 2. Write default .veluneignore if one doesn't already exist
    veluneignore_path = path / ".veluneignore"
    if not veluneignore_path.exists():
        from velune.repository.scanner import DEFAULT_VELUNEIGNORE

        veluneignore_path.write_text(DEFAULT_VELUNEIGNORE, encoding="utf-8")
        if not cli_context.json_mode:
            console.print(
                "[green]✓[/green] Created default .veluneignore (edit to customise index exclusions)."
            )

    from velune.core.event_loop import submit

    submit(_workspace_init_async(cli_context, path, velune_dir, force))


async def _workspace_init_async(
    cli_context: CLIContext,
    path: Path,
    velune_dir: Path,
    force: bool,
) -> None:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    repo_cognition = container.get("runtime.repository_cognition")

    # 3. Boot subsystems and compile index
    await lifecycle.startup()

    if not cli_context.json_mode:
        console.print(
            "[bold cyan]⠋[/bold cyan] Building Tree-sitter compiler AST indices and scanning imports..."
        )
        with console.status(
            "[bold magenta]⚡ Parsing symbols, dependencies, and git Authorship...[/bold magenta]"
        ):
            snapshot = repo_cognition.index(force=force)
    else:
        snapshot = repo_cognition.index(force=force)

    # Calculate statistics
    num_files = len(snapshot.files)
    num_symbols = len(snapshot.symbols)
    num_edges = len(snapshot.edges)
    skipped_secrets = snapshot.summary.get("skipped_secrets", [])

    languages: dict[str, int] = {}
    for f in snapshot.files:
        languages[f.language.value] = languages.get(f.language.value, 0) + 1

    lang_summary = ", ".join(f"{count} {lang}" for lang, count in languages.items())
    git_branch = snapshot.summary.get("git", {}).get("active_branch", "untracked")

    # 4. Render gorgeous Success Summary Panel
    if cli_context.json_mode:
        import json

        print(
            json.dumps(
                {
                    "success": True,
                    "workspace_path": str(path),
                    "caches_directory": str(velune_dir),
                    "indexed_files": num_files,
                    "languages": languages,
                    "parsed_ast_symbols": num_symbols,
                    "dependency_edges": num_edges,
                    "active_branch": git_branch,
                    "skipped_secrets": skipped_secrets,
                }
            )
        )
    else:
        console.print()
        console.print(
            Panel(
                Text.assemble(
                    ("[bold green]✓ VELUNE WORKSPACE SUCCESSFULLY INDEXED[/bold green]\n\n"),
                    (f"[bold]Workspace path:[/bold] {path}\n"),
                    (f"[bold]Caches directory:[/bold] {velune_dir}\n"),
                    (
                        f"[bold]Indexed files:[/bold] {num_files} ({lang_summary or 'no code files found'})\n"
                    ),
                    (f"[bold]Parsed AST symbols:[/bold] {num_symbols} classes/functions/imports\n"),
                    (f"[bold]Dependency edges:[/bold] {num_edges} import link(s)\n"),
                    (f"[bold]Active branch:[/bold] [magenta]{git_branch}[/magenta]\n\n"),
                    (
                        "[dim]Velune repository cognitive engine is primed. Use 'velune run' to start autonomous edits.[/dim]"
                    ),
                ),
                border_style="green",
                box=ROUNDED,
                title="[bold green]Cognitive Priming Success[/bold green]",
            )
        )

        if skipped_secrets:
            secret_lines = "\n".join(f"  [bold yellow]•[/bold yellow] {p}" for p in skipped_secrets)
            console.print(
                Panel(
                    Text.from_markup(
                        "[bold yellow]Velune detected and protected the following files from being indexed:[/bold yellow]\n\n"
                        + secret_lines
                        + "\n\n[dim]These files matched known secrets/credentials patterns. "
                        "Add them to [bold].veluneignore[/bold] to silence this notice, "
                        "or ensure they are listed in [bold].gitignore[/bold].[/dim]"
                    ),
                    title="[bold yellow]🔒 Secrets Protected[/bold yellow]",
                    border_style="yellow",
                    box=ROUNDED,
                    padding=(1, 2),
                )
            )

    await lifecycle.shutdown()


@workspace_cmd.command("status")
def workspace_status(
    ctx: typer.Context,
    path: Path = typer.Option(Path.cwd(), "--path", "-p", help="Workspace path"),
) -> None:
    """Show active workspace structure and index summary."""
    velune_dir = path / ".velune"

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not velune_dir.exists():
        if cli_context.json_mode:
            import json

            print(json.dumps({"error": "Not a Velune workspace (no .velune directory detected)"}))
        else:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import WorkspaceNotInitializedError

            console.print(
                render_error(
                    WorkspaceNotInitializedError(
                        cause_override=f"No .velune directory found in {path}."
                    )
                )
            )
        return

    from velune.core.event_loop import submit

    submit(_workspace_status_async(cli_context, path, velune_dir))


async def _workspace_status_async(
    cli_context: CLIContext,
    path: Path,
    velune_dir: Path,
) -> None:
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    repo_cognition = container.get("runtime.repository_cognition")

    await lifecycle.startup()

    if not cli_context.json_mode:
        with console.status("[bold cyan]Querying cognitive index status...[/bold cyan]"):
            snapshot = repo_cognition.index(force=False)
    else:
        snapshot = repo_cognition.index(force=False)

    num_files = len(snapshot.files)
    num_symbols = len(snapshot.symbols)
    git_branch = snapshot.summary.get("git", {}).get("active_branch", "untracked")

    if cli_context.json_mode:
        print(
            json.dumps(
                {
                    "workspace_root": str(path),
                    "velune_cache": str(velune_dir),
                    "indexed_files_count": num_files,
                    "indexed_symbols_count": num_symbols,
                    "git_branch": git_branch,
                    "status": "Active & Fully Primed",
                }
            )
        )
    else:
        console.print(
            Panel(
                Text.assemble(
                    (f"[bold]Workspace root:[/bold] {path}\n"),
                    (f"[bold]Velune cache:[/bold] {velune_dir}\n"),
                    (f"[bold]Indexed files count:[/bold] {num_files}\n"),
                    (f"[bold]Indexed symbols count:[/bold] {num_symbols}\n"),
                    (f"[bold]Git branch:[/bold] [magenta]{git_branch}[/magenta]\n"),
                    ("[bold]Status:[/bold] [bold green]Active & Fully Primed[/bold green]"),
                ),
                border_style="cyan",
                box=ROUNDED,
                title="[bold cyan]Workspace Status[/bold cyan]",
            )
        )

    await lifecycle.shutdown()


@workspace_cmd.command("graph")
def workspace_graph(
    ctx: typer.Context,
    path: Path = typer.Option(Path.cwd(), "--path", "-p", help="Workspace path"),
    focus: str = typer.Option(
        None, "--focus", "-f", help="Centre the graph on a file (path or suffix, e.g. cognition.py)"
    ),
    depth: int = typer.Option(2, "--depth", "-d", min=1, max=6, help="Focus tree expansion depth"),
    limit: int = typer.Option(15, "--limit", "-l", min=1, max=100, help="Hotspot rows to show"),
    edge_type: str = typer.Option(
        "imports", "--edge-type", help="Edge type to graph (imports, calls, contains)"
    ),
) -> None:
    """Render the real module-dependency graph from the indexed import edges.

    Shows fan-in/fan-out hotspots and import cycles derived from
    ``RepositorySnapshot.edges`` — the same edges Velune builds while indexing.
    Use ``--focus`` to inspect one file's dependencies and dependents.
    """
    velune_dir = path / ".velune"

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not velune_dir.exists():
        if cli_context.json_mode:
            print(json.dumps({"error": "Not a Velune workspace (no .velune directory detected)"}))
        else:
            from velune.cli.rendering.error_panel import render_error
            from velune.core.errors.catalog import WorkspaceNotInitializedError

            console.print(
                render_error(
                    WorkspaceNotInitializedError(
                        cause_override=f"No .velune directory found in {path}."
                    )
                )
            )
        return

    from velune.core.event_loop import submit

    submit(_workspace_graph_async(cli_context, path, focus, depth, limit, edge_type))


async def _workspace_graph_async(
    cli_context: CLIContext,
    path: Path,
    focus: str | None,
    depth: int,
    limit: int,
    edge_type: str,
) -> None:
    from velune.observability.workspace_graph import build_dependency_graph

    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    repo_cognition = container.get("runtime.repository_cognition")

    await lifecycle.startup()
    try:
        if not cli_context.json_mode:
            with console.status("[bold cyan]Building dependency graph from index...[/bold cyan]"):
                snapshot = repo_cognition.index(force=False)
        else:
            snapshot = repo_cognition.index(force=False)

        report = build_dependency_graph(
            snapshot, edge_type=edge_type, focus=focus, depth=depth, top_n=limit
        )

        if cli_context.json_mode:
            print(json.dumps(report.to_dict()))
        else:
            _render_graph(console, report)
    finally:
        await lifecycle.shutdown()


def _render_graph(console: Console, report: DependencyGraphReport) -> None:
    """Render the dependency-graph report as calm, infrastructure-grade panels."""
    # --- Summary header ---
    header = Text()
    header.append("Workspace  ", style=design.MUTED)
    header.append(report.root + "\n", style=f"bold {design.ACCENT}")
    header.append("Edge type  ", style=design.MUTED)
    header.append(f"{report.edge_type}\n", style=design.INFO)
    header.append("Files      ", style=design.MUTED)
    header.append(f"{report.file_count}", style="bold")
    header.append("   Connected ", style=design.MUTED)
    header.append(f"{report.node_count}", style="bold")
    header.append("   Edges ", style=design.MUTED)
    header.append(f"{report.edge_count}", style="bold")
    header.append("   Orphans ", style=design.MUTED)
    orphan_style = design.WARN if report.orphan_count else design.MUTED
    header.append(f"{report.orphan_count}", style=orphan_style)
    if report.edge_type_breakdown:
        bd = "  ".join(f"{et}:{n}" for et, n in report.edge_type_breakdown)
        header.append("\nAll edges  ", style=design.MUTED)
        header.append(bd, style=design.FAINT)
    console.print(
        Panel(
            header,
            border_style=design.ACCENT_SOFT,
            box=ROUNDED,
            title="[bold]Dependency Graph[/bold]",
        )
    )

    if report.node_count == 0:
        console.print(
            f"[{design.MUTED}]No '{report.edge_type}' edges in the index. "
            f"Try --edge-type contains, or run `velune workspace init` to (re)index.[/]"
        )
        return

    # --- Focus view takes priority when requested ---
    if report.focus is not None:
        _render_focus(console, report.focus)
        return
    if report.focus_candidates:
        console.print(f"[{design.WARN}]Ambiguous focus — multiple files match. Candidates:[/]")
        for cand in report.focus_candidates:
            console.print(f"  [{design.MUTED}]•[/] {cand}")
        return

    # --- Hotspot tables ---
    console.print()
    console.print(_hotspot_table("Most depended-upon (fan-in)", report.top_fan_in, "fan_in"))
    console.print()
    console.print(_hotspot_table("Most dependencies (fan-out)", report.top_fan_out, "fan_out"))

    # --- Import cycles ---
    console.print()
    if report.cycles:
        cyc = Text()
        cyc.append(
            f"{len(report.cycles)} import cycle(s) detected\n\n", style=f"bold {design.WARN}"
        )
        for i, cycle in enumerate(report.cycles, 1):
            cyc.append(f"{i}. ", style=design.MUTED)
            cyc.append(" → ".join(cycle) + " → …\n", style=design.DANGER)
        console.print(
            Panel(cyc, border_style=design.WARN, box=ROUNDED, title="[bold]Import Cycles[/bold]")
        )
    else:
        console.print(f"[{design.OK}]✓ No import cycles detected.[/]")


def _hotspot_table(title: str, stats: list[GraphNodeStat], key: str) -> Table:
    table = Table(
        box=ROUNDED, border_style=design.FAINT, title=f"[bold]{title}[/bold]", expand=False
    )
    table.add_column("File", style=design.INFO, no_wrap=False)
    table.add_column("fan-in", justify="right", style=design.MUTED)
    table.add_column("fan-out", justify="right", style=design.MUTED)
    for s in stats:
        primary = design.HIGHLIGHT if getattr(s, key) > 0 else design.MUTED
        table.add_row(
            s.path,
            Text(str(s.fan_in), style=primary if key == "fan_in" else design.MUTED),
            Text(str(s.fan_out), style=primary if key == "fan_out" else design.MUTED),
        )
    return table


def _render_focus(console: Console, focus: FocusView) -> None:
    console.print()
    summary = Text()
    summary.append(focus.node, style=f"bold {design.ACCENT}")
    summary.append(
        f"   imports {len(focus.imports)} · imported by {len(focus.imported_by)}\n",
        style=design.MUTED,
    )
    console.print(summary)

    tree = Tree(f"[bold {design.ACCENT}]{focus.node}[/]", guide_style=design.FAINT)
    _attach_tree(tree, focus.tree)
    if not focus.tree:
        tree.add(f"[{design.MUTED}](no outgoing imports)[/]")
    console.print(
        Panel(
            tree,
            border_style=design.ACCENT_SOFT,
            box=ROUNDED,
            title=f"[bold]Imports (downstream, depth {focus.depth})[/bold]",
        )
    )

    if focus.imported_by:
        body = Text()
        for dep in focus.imported_by:
            body.append("← ", style=design.MUTED)
            body.append(dep + "\n", style=design.INFO)
        console.print(
            Panel(
                body,
                border_style=design.FAINT,
                box=ROUNDED,
                title="[bold]Imported by (upstream)[/bold]",
            )
        )
    else:
        console.print(f"[{design.MUTED}]Nothing imports this file (entry point or leaf).[/]")


def _attach_tree(branch: Tree, children: dict) -> None:
    """Recursively attach the nested focus tree dict to a rich Tree."""
    for name, sub in children.items():
        if isinstance(sub, dict) and sub.get("__cycle__"):
            branch.add(f"[{design.DANGER}]{name} ↺ (cycle)[/]")
            continue
        node = branch.add(f"[{design.INFO}]{name}[/]")
        if isinstance(sub, dict) and sub:
            _attach_tree(node, sub)
