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


# Repository markers that indicate *workspace* is (probably) a project root.
# Detection is advisory only — it NEVER triggers cognition (Rule 12).
_REPO_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")


def _detect_repo_marker(path: Path) -> str | None:
    """Return the directory name if *path* looks like a project root, else None."""
    try:
        for marker in _REPO_MARKERS:
            if (path / marker).exists():
                return path.name or str(path)
    except Exception:
        pass
    return None


def _show_welcome_guide(console: Console) -> None:
    """First-launch guidance — next steps only, no repository processing (Rule 13)."""
    console.print(
        Panel(
            f"[bold {design.ACCENT}]Welcome to Velune[/bold {design.ACCENT}]\n\n"
            f"[{design.INFO}]Next steps[/{design.INFO}]\n"
            "  [bold]1.[/bold] [bold]/model discover[/bold]      [dim]find local + cloud models[/dim]\n"
            "  [bold]2.[/bold] [bold]/model connect[/bold]       [dim]set your default model[/dim]\n"
            "  [bold]3.[/bold] [bold]/project open <path>[/bold] [dim]choose a workspace[/dim]\n"
            "  [bold]4.[/bold] [bold]/index[/bold]               [dim]index the workspace[/dim]\n\n"
            "[dim]Type [bold]/help[/bold] for all commands.[/dim]",
            border_style=design.ACCENT,
            padding=(0, 2),
        )
    )


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

        # Rendering help text never touches the runtime. When the user asks for
        # help on any subcommand (`velune <cmd> --help`), Typer still runs this
        # root callback first — skip the expensive full-subsystem bootstrap so
        # help stays fast (~1.5s instead of ~4s). The subcommand then prints its
        # own help and exits without ever reading ctx.obj.
        if any(arg in ("--help", "-h") for arg in sys.argv[1:]):
            ctx.obj = None
            return

        # Developers can opt into full internal logs with --verbose/-v.
        if verbose:
            logging.getLogger("velune").setLevel(logging.DEBUG)
        else:
            logging.getLogger("velune").setLevel(logging.WARNING)

        if yes:
            from velune.execution.diff_preview import configure as _configure_diff

            _configure_diff(auto_accept=True)

        try:
            from velune.core.runtime import build_runtime

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
                    # In a non-interactive context (piped stdin, CI, cron) there is
                    # no TTY to read a confirmation from and the REPL cannot run.
                    # Print the actionable hint and exit cleanly instead of
                    # blocking on `typer.confirm` (which would hit EOF) or dropping
                    # into a doomed REPL.
                    if not sys.stdin.isatty():
                        runtime.console.print(
                            f"[{design.INFO}]→ Run `velune setup` to configure a provider, "
                            f"then start Velune again.[/{design.INFO}]"
                        )
                        raise typer.Exit()

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

                # First-launch guidance (no model yet) — never processes the repo.
                if not configured:
                    _show_welcome_guide(runtime.console)

                # Advisory repo detection (Rule 12): hint, never auto-cognition.
                repo_name = _detect_repo_marker(workspace)
                if repo_name:
                    runtime.console.print(
                        f"[{design.INFO}]Repository detected:[/{design.INFO}] "
                        f"[cyan]{repo_name}[/cyan]  "
                        f"[dim]→ run [bold]/project open .[/bold], then "
                        f"[bold]/index[/bold][/dim]"
                    )

                _startup_mark("REPL handoff (prompt visible)")
                from velune.kernel.entrypoint import launch

                launch(runtime)

    register_commands(app, ServiceContainer())
    return app


_app_singleton: typer.Typer | None = None


def __getattr__(name: str) -> typer.Typer:
    """Build the Typer app on first attribute access, not at import time.

    Importing this module used to eagerly call ``create_app()`` — which imports
    every command module and the runtime — even for ``velune --version``. The
    app is now constructed lazily and cached so ``from velune.cli.app import app``
    still works for backward compatibility while cheap entry paths pay nothing.
    """
    global _app_singleton
    if name == "app":
        if _app_singleton is None:
            _app_singleton = create_app()
        return _app_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
