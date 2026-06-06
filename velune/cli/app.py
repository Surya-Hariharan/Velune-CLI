"""Typer application factory for Velune."""

from __future__ import annotations

import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from velune import __version__
from velune.cli.context import CLIContext
from velune.cli.registry import register_commands
from velune.core.runtime import build_runtime
from velune.kernel.registry import ServiceContainer


def _startup_frames(workspace: Path, config_path: Path | None) -> list[Panel]:
    banner = """
██╗   ██╗███████╗██╗     ██╗   ██╗███╗   ██╗███████╗
██║   ██║██╔════╝██║     ██║   ██║████╗  ██║██╔════╝
██║   ██║█████╗  ██║     ██║   ██║██╔██╗ ██║█████╗  
╚██╗ ██╔╝██╔══╝  ██║     ██║   ██║██║╚██╗██║██╔══╝  
 ╚████╔╝ ███████╗███████╗╚██████╔╝██║ ╚████║███████╗
  ╚═══╝  ╚══════╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝
""".strip("\n")

    frames: list[Panel] = []
    lines = banner.splitlines()
    for index in range(1, len(lines) + 1):
        body = "\n".join(lines[:index])
        if index == len(lines):
            body += "\n\n[bold cyan]Welcome to Velune CLI![/bold cyan]\n[dim]v" + __version__ + "[/dim]\n\n[bold]What would you like to build today?[/bold]"
        frames.append(
            Panel(
                Text.from_markup(body),
                title="Velune",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    frames.append(
        Panel(
            Text.from_markup(
                "[bold cyan]Welcome to Velune CLI![/bold cyan]\n"
                f"[dim]v{__version__}[/dim]\n\n"
                "[bold]What would you like to build today?[/bold]"
            ),
            title="Velune",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    return frames


def _check_ollama_live() -> bool:
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _show_startup_animation(console: Console, workspace: Path, config_path: Path | None) -> None:
    """Show startup animation only in interactive TTY sessions."""
    import sys
    if not sys.stdout.isatty():
        return  # Skip animation in CI, piped output, --quiet mode
    
    frames = _startup_frames(workspace, config_path)
    with Live(frames[0], console=console, refresh_per_second=12, transient=True) as live:
        for frame in frames[1:]:
            live.update(frame)
            time.sleep(0.08)  # Acceptable: sync context, interactive only


def create_app() -> typer.Typer:
    """Create the root Typer application."""

    app = typer.Typer(
        name="velune",
        help="Terminal-first cognitive AI orchestration system",
        no_args_is_help=False,
        add_completion=True,
        rich_markup_mode="rich",
    )

    @app.callback(invoke_without_command=True)
    def main(
        ctx: typer.Context,
        workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", help="Workspace root"),
        config_path: Path | None = typer.Option(None, "--config", "-c", help="Explicit velune.toml path"),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
        version: bool = typer.Option(False, "--version", help="Show version and exit"),
        json_mode: bool = typer.Option(False, "--json", help="Enable machine-readable JSON output mode"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Auto-accept all file changes without prompting"),
    ) -> None:
        """Initialize process-wide runtime state for every CLI invocation."""

        if version:
            if json_mode:
                import json
                print(json.dumps({"version": __version__}))
            else:
                Console().print(f"Velune v{__version__}")
            raise typer.Exit()

        if yes:
            from velune.execution.diff_preview import configure as _configure_diff
            _configure_diff(auto_accept=True)

        try:
            runtime = build_runtime(workspace=workspace, config_path=config_path, verbose=verbose)
        except Exception as e:
            if json_mode:
                import json
                print(json.dumps({"error": f"Velune failed to start: {e}"}))
            else:
                Console().print(
                    f"[bold red]Velune failed to start:[/bold red] {e}\n"
                    "Run [bold cyan]`velune doctor check`[/bold cyan] to diagnose the issue."
                )
            raise typer.Exit(1)

        runtime.container.register_instance("runtime.auto_accept", yes)

        ctx.obj = CLIContext(
            workspace=workspace,
            config_path=config_path,
            verbose=verbose,
            runtime=runtime,
            json_mode=json_mode,
            yes=yes,
        )

        if ctx.invoked_subcommand is None:
            if json_mode:
                import json
                print(json.dumps({
                    "status": "ready",
                    "workspace": str(workspace),
                    "config_path": str(config_path) if config_path else None,
                    "version": __version__
                }))
            else:
                from velune.providers.keystore import list_configured_providers
                configured = list_configured_providers()
                ollama_live = _check_ollama_live()

                if not configured and not ollama_live:
                    runtime.console.print(Panel(
                        "[yellow]No AI providers configured.[/yellow]\n"
                        "[dim]Velune needs at least one provider to work.[/dim]",
                        border_style="yellow",
                    ))
                    run_setup = typer.confirm("Run setup now?", default=True)
                    if run_setup:
                        from velune.cli.commands.setup import run_setup_wizard
                        run_setup_wizard()
                    else:
                        runtime.console.print(
                            "[dim]Run `velune setup` any time to configure providers.[/dim]"
                        )
                    raise typer.Exit(0)

                _show_startup_animation(runtime.console, workspace, config_path)
                from velune.cli.repl import run_repl
                run_repl(runtime)

    register_commands(app, ServiceContainer())
    return app


app = create_app()
