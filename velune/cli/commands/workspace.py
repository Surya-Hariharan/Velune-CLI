"""Workspace commands — velune workspace init/status/graph/list/open/remove."""

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
from velune.core.redaction import redact_secrets

if TYPE_CHECKING:
    from velune.observability.workspace_graph import (
        DependencyGraphReport,
        FocusView,
        GraphNodeStat,
    )

console = Console()
workspace_cmd = typer.Typer(help="Detect, index, remember, and resume projects.")


@workspace_cmd.command("init")
def workspace_init(
    ctx: typer.Context,
    path: Path = typer.Argument(Path.cwd(), help="Workspace path"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reinitialization and re-index"),
) -> None:
    """(Re)build the Tree-sitter AST index for a workspace.

    For first-time project setup prefer `velune init`, which also detects your
    hardware, writes config, and updates .gitignore before delegating to this
    same indexing routine. Use `velune workspace init` to re-index an existing
    workspace (e.g. after large changes, or with `--force`).
    """
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
        console.print("[green]Created .velune configuration directory structure.[/green]")

    # 2. Write default .veluneignore if one doesn't already exist
    veluneignore_path = path / ".veluneignore"
    if not veluneignore_path.exists():
        from velune.repository.scanner import DEFAULT_VELUNEIGNORE

        veluneignore_path.write_text(DEFAULT_VELUNEIGNORE, encoding="utf-8")
        if not cli_context.json_mode:
            console.print(
                "[green]Created default .veluneignore (edit to customise index exclusions).[/green]"
            )

    # Register in the global workspace registry so `velune workspace list` can
    # find this project immediately after init, without needing a chat/run first.
    try:
        from velune.cli.workspaces import WorkspaceRegistry

        WorkspaceRegistry().register(path)
    except Exception:
        pass

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
            "[bold cyan]Building Tree-sitter compiler AST indices and scanning imports...[/bold cyan]"
        )
        with console.status(
            "[bold magenta]Parsing symbols, dependencies, and git authorship...[/bold magenta]"
        ):
            snapshot = repo_cognition.index(force=force)
    else:
        snapshot = repo_cognition.index(force=force)

    # Calculate statistics
    num_files = len(snapshot.files)
    num_symbols = len(snapshot.symbols)
    num_edges = len(snapshot.edges)
    # These are file *paths* excluded from indexing because they matched known
    # secrets/credentials filename patterns (e.g. .env, credentials.json).
    # Sanitize them in case a credential-shaped value appears in a path component.
    excluded_file_paths: list[str] = [
        redact_secrets(str(p)) for p in snapshot.summary.get("excluded_paths", [])
    ]

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
                    "excluded_file_paths": excluded_file_paths,
                }
            )
        )
    else:
        console.print()
        console.print(
            Panel(
                Text.from_markup(
                    "[bold green]VELUNE WORKSPACE SUCCESSFULLY INDEXED[/bold green]\n\n"
                    f"[bold]Workspace path:[/bold] {path}\n"
                    f"[bold]Caches directory:[/bold] {velune_dir}\n"
                    f"[bold]Indexed files:[/bold] {num_files} ({lang_summary or 'no code files found'})\n"
                    f"[bold]Parsed AST symbols:[/bold] {num_symbols} classes/functions/imports\n"
                    f"[bold]Dependency edges:[/bold] {num_edges} import link(s)\n"
                    f"[bold]Active branch:[/bold] [magenta]{git_branch}[/magenta]\n\n"
                    "[dim]Velune repository cognitive engine is primed."
                    " Use 'velune run' to start autonomous edits.[/dim]"
                ),
                border_style="green",
                box=ROUNDED,
                title="[bold green]Cognitive Priming Success[/bold green]",
            )
        )

        if excluded_file_paths:
            secret_lines = "\n".join(
                f"  [{design.WARN}]{p}[/{design.WARN}]" for p in excluded_file_paths
            )
            console.print(
                Panel(
                    Text.from_markup(
                        "[bold yellow]Velune detected and protected the following files from being indexed:[/bold yellow]\n\n"
                        + secret_lines
                        + "\n\n[dim]These files matched known secrets/credentials patterns. "
                        "Add them to [bold].veluneignore[/bold] to silence this notice, "
                        "or ensure they are listed in [bold].gitignore[/bold].[/dim]"
                    ),
                    title="[bold yellow]Secrets Protected[/bold yellow]",
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
            console.print(f"  [{design.MUTED}]{cand}[/{design.MUTED}]")
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
        console.print(f"[{design.OK}]No import cycles detected.[/]")


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


# ── Directory tree view ──────────────────────────────────────────────────────


@workspace_cmd.command("tree")
def workspace_tree(
    ctx: typer.Context,
    path: Path = typer.Option(Path.cwd(), "--path", "-p", help="Workspace path"),
    depth: int = typer.Option(3, "--depth", "-d", min=1, max=10, help="Max depth to display"),
    all_files: bool = typer.Option(
        False, "--all", "-a", help="Show all files including ignored ones"
    ),
    dirs_only: bool = typer.Option(False, "--dirs", help="Show directories only"),
) -> None:
    """Render the workspace directory tree, respecting .gitignore and .veluneignore rules."""
    from velune.repository.scanner import FilesystemScanner

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    root = path.resolve()
    if not root.exists():
        console.print(f"[{design.DANGER}]Path not found: {root}[/]")
        raise typer.Exit(1)

    scanner = FilesystemScanner(root) if not all_files else None

    def _is_ignored(p: Path) -> bool:
        return False if scanner is None else scanner.is_ignored(p)

    file_count = 0
    dir_count = 0

    def _build(branch: Tree, current: Path, current_depth: int) -> None:
        nonlocal file_count, dir_count
        if current_depth > depth:
            return
        try:
            items = sorted(current.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            branch.add(f"[{design.DANGER}](permission denied)[/]")
            return

        for item in items:
            if _is_ignored(item):
                continue
            if item.is_dir():
                dir_count += 1
                sub = branch.add(f"[bold {design.ACCENT}]{item.name}/[/]")
                _build(sub, item, current_depth + 1)
            elif not dirs_only and item.is_file():
                file_count += 1
                # Colour by extension type
                suffix = item.suffix.lower()
                if suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"}:
                    style = design.INFO
                elif suffix in {".md", ".txt", ".rst"}:
                    style = design.MUTED
                elif suffix in {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"}:
                    style = design.HIGHLIGHT
                else:
                    style = design.FAINT
                branch.add(f"[{style}]{item.name}[/]")

    tree = Tree(
        f"[bold {design.ACCENT}]{root.name}/[/]",
        guide_style=design.FAINT,
    )
    _build(tree, root, 1)

    console.print(tree)
    console.print()
    console.print(
        f"[{design.MUTED}]{dir_count} director{'ies' if dir_count != 1 else 'y'}  ·  "
        f"{file_count} file{'s' if file_count != 1 else ''}[/]  "
        f"[{design.FAINT}](depth {depth}{'  ·  all files shown' if all_files else ''})[/]"
    )


# ── Multi-workspace management ────────────────────────────────────────────────


@workspace_cmd.command("list")
def workspace_list(ctx: typer.Context) -> None:
    """List all registered Velune workspaces."""
    from velune.cli.workspaces import WorkspaceRegistry

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    registry = WorkspaceRegistry()
    workspaces = registry.list()

    if cli_context.json_mode:
        from dataclasses import asdict

        print(json.dumps([asdict(w) for w in workspaces]))
        return

    if not workspaces:
        console.print(
            f"[{design.MUTED}]No workspaces registered yet.[/]\n"
            f"[{design.MUTED}]Run[/] [bold]velune workspace init[/bold] [{design.MUTED}]inside a project to register it.[/]"
        )
        return

    current = str(cli_context.workspace.resolve()).lower()

    table = Table(box=ROUNDED, border_style=design.FAINT, expand=False)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Name", style=design.ACCENT)
    table.add_column("Path", style=design.INFO)
    table.add_column("Type", style=design.MUTED)
    table.add_column("Git", style=design.MUTED, justify="center")
    table.add_column("Last opened", style=design.MUTED, no_wrap=True)

    for w in workspaces:
        is_active = w.path.lower() == current
        marker = Text("*", style=design.OK) if is_active else Text("")
        table.add_row(
            marker,
            w.name,
            w.path,
            w.project_type or "—",
            "yes" if w.is_git else "—",
            w.last_opened[:16].replace("T", " "),
        )

    console.print(table)
    console.print()
    console.print(
        f"[{design.MUTED}]* = current workspace  ·  "
        f"Switch with:[/] [bold]velune workspace open <path>[/bold]"
    )


def _render_workspace_entry(info, *, title: str, extra: list[str] | None = None) -> None:
    """Show a workspace's identity and the exact commands to work in it.

    Each ``velune`` invocation resolves its own workspace from the working
    directory (or ``--workspace``), so — unlike the in-process REPL ``/project``
    switch — the CLI cannot change the parent shell's directory. Instead we hand
    back copy-pasteable entry commands. ``extra`` appends context-specific lines
    (e.g. a session to resume) above the generic tips.
    """
    lines: list[str] = [
        f"[bold]Name:[/bold]   {info.name}\n",
        f"[bold]Path:[/bold]   {info.path}\n",
        f"[bold]Type:[/bold]   {info.project_type or 'unknown'}\n",
        f"[bold]Git:[/bold]    {'yes' if info.is_git else 'no'}\n\n",
    ]
    for line in extra or []:
        lines.append(line)
    lines.extend(
        [
            f"[{design.MUTED}]To work in this workspace from any command:[/]\n",
            f"  velune --workspace {info.path} chat\n",
            f"  velune --workspace {info.path} run <task>\n\n",
            f"[{design.MUTED}]Or cd into the project and run velune directly.[/]",
        ]
    )
    console.print(
        Panel(
            Text.from_markup("".join(lines)),
            border_style=design.ACCENT_SOFT,
            box=ROUNDED,
            title=f"[bold]{title}[/bold]",
        )
    )


def _resolve_workspace_target(registry, target: str) -> Path | None:
    """Resolve *target* as a registered workspace name first, else a path.

    Mirrors the REPL's ``/project`` resolution so ``velune workspace open myproj``
    works by name, not just by filesystem path. Returns ``None`` if neither a
    known name nor an existing directory matches.
    """
    info = registry.find_by_name(target)
    if info is not None:
        return Path(info.path)
    candidate = Path(target).expanduser()
    if candidate.exists():
        return candidate.resolve()
    return None


@workspace_cmd.command("open")
def workspace_open(
    ctx: typer.Context,
    target: str = typer.Argument(
        ..., help="Registered workspace name, or a path to the project directory"
    ),
    init: bool = typer.Option(
        False, "--init", "-i", help="Also run workspace init to index the project"
    ),
) -> None:
    """Register a workspace (by name or path) and print how to work in it.

    Velune resolves the active workspace per invocation from the working
    directory (or ``--workspace``). This command registers the target in the
    global registry and shows you how to target it in any velune command. The
    target may be a previously-registered name or a filesystem path.
    """
    from velune.cli.workspaces import WorkspaceRegistry

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    registry = WorkspaceRegistry()
    resolved = _resolve_workspace_target(registry, target)
    if resolved is None:
        if cli_context.json_mode:
            print(json.dumps({"error": f"Unknown workspace name or path: {target}"}))
        else:
            console.print(f"[{design.DANGER}]Unknown workspace name or path: {target}[/]")
            console.print(
                f"[{design.MUTED}]Run[/] [bold]velune workspace list[/bold] "
                f"[{design.MUTED}]to see registered workspaces.[/]"
            )
        raise typer.Exit(1)

    info = registry.register(resolved)

    if cli_context.json_mode:
        from dataclasses import asdict

        print(json.dumps(asdict(info)))
    else:
        _render_workspace_entry(info, title=f"Workspace registered — {info.name}")

    if init:
        velune_dir = resolved / ".velune"
        velune_dir.mkdir(exist_ok=True)
        for sub in ("memory", "retrieval", "index", "snapshots"):
            (velune_dir / sub).mkdir(exist_ok=True)

        from velune.core.event_loop import submit

        submit(_workspace_init_async(cli_context, resolved, velune_dir, force=False))


@workspace_cmd.command("resume")
def workspace_resume(
    ctx: typer.Context,
    name: str = typer.Argument(
        None, help="Workspace name or path to resume. Omit to resume the most recent."
    ),
) -> None:
    """Reopen a recent workspace and surface its latest session to continue.

    With no argument this picks the most recently used workspace from the
    registry. It marks the workspace as opened again and, if it has saved
    sessions, prints the exact command to jump back into the last conversation —
    tying workspace resume to session resume.
    """
    from velune.cli.workspaces import WorkspaceRegistry

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    registry = WorkspaceRegistry()

    if name:
        resolved = _resolve_workspace_target(registry, name)
        info = registry.get(resolved) if resolved is not None else None
        if info is None and resolved is not None:
            # A real directory that wasn't registered yet — register it now.
            info = registry.register(resolved)
    else:
        workspaces = registry.list()
        info = workspaces[0] if workspaces else None

    if info is None:
        if cli_context.json_mode:
            print(json.dumps({"error": "No workspace to resume"}))
        else:
            console.print(f"[{design.MUTED}]No workspace to resume.[/]")
            console.print(
                f"[{design.MUTED}]Run[/] [bold]velune init[/bold] "
                f"[{design.MUTED}]inside a project first.[/]"
            )
        raise typer.Exit(1)

    ws_path = Path(info.path)
    registry.touch(ws_path)

    # Tie into session resume: find the newest saved session for this workspace.
    latest_session = None
    try:
        from velune.cli.sessions import SessionStore

        recent = SessionStore().list(workspace=str(ws_path.resolve()), limit=1)
        latest_session = recent[0] if recent else None
    except Exception:
        latest_session = None

    if cli_context.json_mode:
        from dataclasses import asdict

        payload = asdict(info)
        payload["latest_session"] = (
            {"id": latest_session.id, "title": latest_session.title}
            if latest_session is not None
            else None
        )
        print(json.dumps(payload))
        return

    extra: list[str] = []
    if latest_session is not None:
        extra.append(f"[{design.MUTED}]Continue your last session:[/]\n")
        extra.append(
            f"  velune --workspace {info.path} chat --session {latest_session.id}"
            f"   [{design.FAINT}]# {latest_session.title}[/]\n\n"
        )
    _render_workspace_entry(info, title=f"Resumed — {info.name}", extra=extra)


@workspace_cmd.command("explain")
def workspace_explain(
    ctx: typer.Context,
    path: Path = typer.Option(Path.cwd(), "--path", "-p", help="Workspace path"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Generate a plain-English architecture summary of this workspace.

    Runs technology detection and architecture classification on the current index
    without calling any AI provider. Shows framework, routing, auth, state management,
    detected features, critical files, and entry points.
    """
    velune_dir = path / ".velune"
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not velune_dir.exists():
        console.print(
            f"[{design.DANGER}]No .velune directory found. Run `velune workspace init` first.[/]"
        )
        raise typer.Exit(1)

    from velune.core.event_loop import submit

    submit(_workspace_explain_async(cli_context, path, velune_dir, json_out))


async def _workspace_explain_async(
    cli_context: CLIContext,
    path: Path,
    velune_dir: Path,
    json_out: bool,
) -> None:
    import json as _json

    from velune.repository.architecture_detector import ArchitectureDetector
    from velune.repository.technology_detector import TechnologyDetector

    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    repo_cognition = container.get("runtime.repository_cognition")

    await lifecycle.startup()
    try:
        with console.status("[bold cyan]Analysing repository architecture...[/bold cyan]"):
            snapshot = repo_cognition.index(force=False)

        tech_detector = TechnologyDetector(path)
        tech = tech_detector.detect()

        arch_detector = ArchitectureDetector(path, snapshot.files, tech)
        arch = arch_detector.detect()

        # Pull richer data from snapshot summary if available (set by _run_pipeline)
        snap_arch = snapshot.summary.get("architecture", {})
        tech_dict = snap_arch.get("tech_stack", tech.to_dict())
        arch_dict = snap_arch.get("arch_report", arch.to_dict())

        if json_out:
            console.print(
                _json.dumps({"tech_stack": tech_dict, "architecture": arch_dict}, indent=2)
            )
            return

        # ── Tech stack panel ───────────────────────────────────────────────
        tech_lines: list[str] = tech.as_summary_lines()
        tech_text = Text()
        for line in tech_lines:
            label, _, value = line.partition(":")
            tech_text.append(label + ":", style=f"bold {design.MUTED}")
            tech_text.append(" " + value.strip() + "\n", style=design.ACCENT)

        if not tech_lines:
            tech_text.append(
                "No technology manifest found (package.json / pyproject.toml / Cargo.toml)\n",
                style=design.MUTED,
            )

        console.print(
            Panel(
                tech_text,
                border_style=design.ACCENT_SOFT,
                box=ROUNDED,
                title=f"[bold]Technology Stack — {path.name}[/bold]",
            )
        )

        # ── Architecture panel ─────────────────────────────────────────────
        arch_text = Text()
        for line in arch.summary_lines():
            label, _, value = line.partition(":")
            arch_text.append(label + ":", style=f"bold {design.MUTED}")
            arch_text.append(" " + value.strip() + "\n", style=design.INFO)

        entry_pts = arch_dict.get("entry_points", [])
        if entry_pts:
            arch_text.append("\nEntry points:\n", style=f"bold {design.MUTED}")
            for ep in entry_pts[:4]:
                arch_text.append(f"  {ep}\n", style=design.HIGHLIGHT)

        console.print(
            Panel(
                arch_text,
                border_style=design.ACCENT_SOFT,
                box=ROUNDED,
                title="[bold]Architecture[/bold]",
            )
        )

        # ── Critical files panel ───────────────────────────────────────────
        critical = arch_dict.get("critical_files", [])
        if critical:
            crit_text = Text()
            crit_text.append("State files\n", style=f"bold {design.MUTED}")
            for f in arch_dict.get("state_files", [])[:4]:
                crit_text.append(f"  {f}\n", style=design.INFO)
            crit_text.append("\nAPI / service files\n", style=f"bold {design.MUTED}")
            for f in arch_dict.get("api_files", [])[:4]:
                crit_text.append(f"  {f}\n", style=design.INFO)
            console.print(
                Panel(
                    crit_text,
                    border_style=design.FAINT,
                    box=ROUNDED,
                    title="[bold]Critical Files[/bold]",
                )
            )

        # ── Import graph summary ───────────────────────────────────────────
        import_edges = [e for e in snapshot.edges if e.edge_type == "imports"]
        n_files = len(snapshot.files)
        n_edges = len(import_edges)
        connected = len({e.source for e in import_edges} | {e.target for e in import_edges})

        graph_line = Text()
        graph_line.append(f"{n_files} files", style="bold")
        graph_line.append("   ", style=design.MUTED)
        graph_line.append(f"{n_edges} import edges", style="bold")
        graph_line.append("   ", style=design.MUTED)
        graph_line.append(f"{connected} connected nodes", style="bold")

        edge_style = design.OK if n_edges > 0 else design.WARN
        console.print(
            Panel(
                graph_line,
                border_style=edge_style,
                box=ROUNDED,
                title="[bold]Import Graph[/bold]",
            )
        )

        console.print(
            f"\n[{design.MUTED}]Tip: `velune workspace graph` to explore the full dependency graph  ·  "
            f'`velune pipeline trace "<query>"` to test retrieval[/]'
        )
    finally:
        await lifecycle.shutdown()


@workspace_cmd.command("forget")
def workspace_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Workspace name to remove from registry"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove a workspace from the registry (does not delete files)."""
    from velune.cli.workspaces import WorkspaceRegistry

    registry = WorkspaceRegistry()
    info = registry.find_by_name(name)
    if info is None:
        console.print(f"[{design.DANGER}]No workspace named '{name}' found in registry.[/]")
        console.print(
            f"[{design.MUTED}]Run[/] [bold]velune workspace list[/bold] "
            f"[{design.MUTED}]to see registered workspaces.[/]"
        )
        raise typer.Exit(1)

    if not yes:
        typer.confirm(
            f"Remove workspace '{name}' ({info.path}) from registry?",
            default=False,
            abort=True,
        )

    registry.remove(name)
    console.print(
        f"[{design.OK}]Workspace [bold]{name}[/bold] removed from registry.[/{design.OK}]\n"
        f"[{design.MUTED}]Project files at {info.path} were not touched.[/]"
    )
