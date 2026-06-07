"""Typer application factory for Velune."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import logging

# Suppress all internal Velune logs from showing in terminal.
# Users see Rich output only — not raw Python logs. This MUST run before
# any velune.* modules are imported so their module-level loggers inherit
# these levels before producing any output.
logging.getLogger("velune").setLevel(logging.WARNING)
logging.getLogger("qdrant_client").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Suppress the root logger from printing INFO/DEBUG to stderr.
logging.getLogger().setLevel(logging.WARNING)

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from velune import __version__
from velune.cli.context import CLIContext
from velune.cli.registry import register_commands
from velune.core.runtime import build_runtime
from velune.core.startup_profiler import mark as _startup_mark
from velune.kernel.registry import ServiceContainer

_startup_mark("cli.app imported (typer/rich/runtime ready)")


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


def _show_startup_banner(console: Console, workspace: Path, config_path: Path | None) -> None:
    """Render the welcome banner once, instantly.

    The previous implementation animated the banner frame-by-frame with a
    ``time.sleep(0.08)`` per line — ~0.6-1.2s of pure blocking delay on every
    interactive launch. Modern AI terminals show their surface immediately;
    perceived speed comes from an instant prompt, not a reveal animation.
    """
    import sys
    if not sys.stdout.isatty():
        return  # Skip banner in CI, piped output, --quiet mode

    console.print(_startup_frames(workspace, config_path)[-1])


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

        # Developers can opt into full internal logs with --verbose/-v.
        if verbose:
            logging.getLogger("velune").setLevel(logging.DEBUG)
        else:
            logging.getLogger("velune").setLevel(logging.WARNING)

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
                # list_configured_providers already includes a (short, deduped)
                # Ollama reachability probe, so a single call suffices.
                configured = list_configured_providers()

                if not configured:
                    runtime.console.print(Panel(
                        "[yellow]No AI providers configured.[/yellow]\n"
                        "[dim]Velune needs at least one provider to work.[/dim]",
                        border_style="yellow",
                    ))
                    run_now = typer.confirm("Run setup now?", default=True)
                    if run_now:
                        from velune.cli.commands.setup import run_setup_wizard
                        run_setup_wizard()
                    else:
                        runtime.console.print(
                            "[dim]Run `velune setup` any time to configure providers.[/dim]"
                        )
                    # NO EXIT HERE — fall through to REPL regardless. The only
                    # ways out of Velune are /exit, /quit, or Ctrl+C twice.

                _show_startup_banner(runtime.console, workspace, config_path)
                _startup_mark("REPL handoff (prompt visible)")
                from velune.cli.repl import run_repl
                run_repl(runtime)

    register_commands(app, ServiceContainer())
    return app


app = create_app()
