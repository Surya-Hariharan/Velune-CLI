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
from velune.cli import design
from velune.cli.context import CLIContext
from velune.cli.registry import register_commands
from velune.core.runtime import build_runtime
from velune.core.startup_profiler import mark as _startup_mark
from velune.kernel.registry import ServiceContainer

_startup_mark("cli.app imported (typer/rich/runtime ready)")


def _startup_frames(workspace: Path, config_path: Path | None) -> list[Panel]:
    body = (
        f"[bold {design.ACCENT}]✦ Velune[/bold {design.ACCENT}]"
        f" [{design.MUTED}]v{__version__}[/{design.MUTED}] · "
        f"[{design.INFO}]Orchestrating your local AI[/{design.INFO}]"
    )
    frame = Panel(
        Text.from_markup(body),
        border_style=design.GREEN,
        padding=(0, 2),
    )
    return [frame]


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
        config_path: Path | None = typer.Option(
            None, "--config", "-c", help="Explicit velune.toml path"
        ),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
        version: bool = typer.Option(False, "--version", help="Show version and exit"),
        json_mode: bool = typer.Option(
            False, "--json", help="Enable machine-readable JSON output mode"
        ),
        yes: bool = typer.Option(
            False, "--yes", "-y", help="Auto-accept all file changes without prompting"
        ),
    ) -> None:
        """Initialize process-wide runtime state for every CLI invocation."""

        if version:
            if json_mode:
                import json

                print(json.dumps({"version": __version__}))
            else:
                Console().print(
                    f"[bold {design.ACCENT}]◆ velune[/bold {design.ACCENT}]"
                    f" [{design.MUTED}]v[/{design.MUTED}]"
                    f"[{design.GREEN}]{__version__}[/{design.GREEN}]"
                )
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
                from velune.cli.rendering.error_panel import render_error, render_unexpected_error
                from velune.core.errors.catalog import VeluneError, WorkspaceNotInitializedError

                if isinstance(e, VeluneError):
                    Console().print(render_error(e))
                elif "velune.toml" in str(e).lower() or "workspace" in str(e).lower():
                    Console().print(render_error(WorkspaceNotInitializedError(str(e))))
                else:
                    Console().print(render_unexpected_error(e))
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

                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "workspace": str(workspace),
                            "config_path": str(config_path) if config_path else None,
                            "version": __version__,
                            "brand": "velune-organic",
                        }
                    )
                )
            else:
                from velune.providers.keystore import list_configured_providers

                # list_configured_providers already includes a (short, deduped)
                # Ollama reachability probe, so a single call suffices.
                configured = list_configured_providers()

                if not configured:
                    runtime.console.print(
                        Panel(
                            f"[{design.WARN}]⚠ No AI providers configured[/{design.WARN}]\n"
                            f"[{design.INFO}]Velune needs at least one provider to orchestrate.[/{design.INFO}]",
                            border_style=design.WARN,
                        )
                    )
                    run_now = typer.confirm("Run setup now?", default=True)
                    if run_now:
                        from velune.cli.commands.setup import run_setup_wizard

                        run_setup_wizard()
                    else:
                        runtime.console.print(
                            f"[{design.INFO}]→ Run `velune setup` any time to configure providers[/{design.INFO}]"
                        )
                    # NO EXIT HERE — fall through to REPL regardless. The only
                    # ways out of Velune are /exit, /quit, or Ctrl+C twice.

                _show_startup_banner(runtime.console, workspace, config_path)
                _startup_mark("REPL handoff (prompt visible)")
                from velune.kernel.entrypoint import launch

                launch(runtime)

    register_commands(app, ServiceContainer())
    return app


app = create_app()
