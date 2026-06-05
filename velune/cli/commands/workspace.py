"""Workspace commands — velune workspace init/status."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune.cli.context import CLIContext

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
            console.print("[green]✓[/green] Created default .veluneignore (edit to customise index exclusions).")

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
        console.print("[bold cyan]⠋[/bold cyan] Building Tree-sitter compiler AST indices and scanning imports...")
        with console.status("[bold magenta]⚡ Parsing symbols, dependencies, and git Authorship...[/bold magenta]") as status:
            snapshot = repo_cognition.index(force=force)
    else:
        snapshot = repo_cognition.index(force=force)

    # Calculate statistics
    num_files = len(snapshot.files)
    num_symbols = len(snapshot.symbols)
    num_edges = len(snapshot.edges)

    languages = {}
    for f in snapshot.files:
        languages[f.language.value] = languages.get(f.language.value, 0) + 1

    lang_summary = ", ".join(f"{count} {lang}" for lang, count in languages.items())
    git_branch = snapshot.summary.get("git", {}).get("active_branch", "untracked")

    # 4. Render gorgeous Success Summary Panel
    if cli_context.json_mode:
        import json
        print(json.dumps({
            "success": True,
            "workspace_path": str(path),
            "caches_directory": str(velune_dir),
            "indexed_files": num_files,
            "languages": languages,
            "parsed_ast_symbols": num_symbols,
            "dependency_edges": num_edges,
            "active_branch": git_branch
        }))
    else:
        console.print()
        console.print(
            Panel(
                Text.assemble(
                    ("[bold green]✓ VELUNE WORKSPACE SUCCESSFULLY INDEXED[/bold green]\n\n"),
                    (f"[bold]Workspace path:[/bold] {path}\n"),
                    (f"[bold]Caches directory:[/bold] {velune_dir}\n"),
                    (f"[bold]Indexed files:[/bold] {num_files} ({lang_summary or 'no code files found'})\n"),
                    (f"[bold]Parsed AST symbols:[/bold] {num_symbols} classes/functions/imports\n"),
                    (f"[bold]Dependency edges:[/bold] {num_edges} import link(s)\n"),
                    (f"[bold]Active branch:[/bold] [magenta]{git_branch}[/magenta]\n\n"),
                    ("[dim]Velune repository cognitive engine is primed. Use 'velune run' to start autonomous edits.[/dim]")
                ),
                border_style="green",
                box=ROUNDED,
                title="[bold green]Cognitive Priming Success[/bold green]"
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
            console.print("[bold red]✗ Not a Velune workspace (no .velune directory detected).[/bold red]")
            console.print("[dim]Use 'velune workspace init' to initialize.[/dim]")
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
        with console.status("[bold cyan]Querying cognitive index status...[/bold cyan]") as status:
            snapshot = repo_cognition.index(force=False)
    else:
        snapshot = repo_cognition.index(force=False)

    num_files = len(snapshot.files)
    num_symbols = len(snapshot.symbols)
    git_branch = snapshot.summary.get("git", {}).get("active_branch", "untracked")

    if cli_context.json_mode:
        import json
        print(json.dumps({
            "workspace_root": str(path),
            "velune_cache": str(velune_dir),
            "indexed_files_count": num_files,
            "indexed_symbols_count": num_symbols,
            "git_branch": git_branch,
            "status": "Active & Fully Primed"
        }))
    else:
        console.print(
            Panel(
                Text.assemble(
                    (f"[bold]Workspace root:[/bold] {path}\n"),
                    (f"[bold]Velune cache:[/bold] {velune_dir}\n"),
                    (f"[bold]Indexed files count:[/bold] {num_files}\n"),
                    (f"[bold]Indexed symbols count:[/bold] {num_symbols}\n"),
                    (f"[bold]Git branch:[/bold] [magenta]{git_branch}[/magenta]\n"),
                    ("[bold]Status:[/bold] [bold green]Active & Fully Primed[/bold green]")
                ),
                border_style="cyan",
                box=ROUNDED,
                title="[bold cyan]Workspace Status[/bold cyan]"
            )
        )

    await lifecycle.shutdown()
