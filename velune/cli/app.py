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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer


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


def create_app(register: str | None = "__all__") -> typer.Typer:
    """Create the root Typer application.

    ``register`` is forwarded to :func:`register_commands` to control how many
    command modules are imported: ``"__all__"`` (every command, the default for
    the back-compat singleton, completion, and tests), ``None`` (none — the bare
    REPL path), or a single command name (lazy-import just that one).
    """
    import typer

    globals()["typer"] = typer
    from rich.console import Console
    from rich.text import Text

    from velune import __version__
    from velune.cli import design
    from velune.cli.context import CLIContext
    from velune.cli.registry import bootstrap_level, register_commands
    from velune.core.startup_profiler import mark as _startup_mark
    from velune.kernel.registry import ServiceContainer

    _startup_mark("cli.app imported (typer/rich/runtime ready)")

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

        # The interactive session (no subcommand) defers the expensive Tier-1
        # subsystems to a background warm-up so the prompt appears instantly.
        # Read-only/diagnostic subcommands tagged ``bootstrap="light"`` (config,
        # doctor, usage, quota, health, logs, status) likewise skip Tier-1 — they
        # consume only Tier-0 (config + providers/models/console), so building
        # memory/retrieval/cognition/orchestration is ~2.2s of pure waste. Every
        # other subcommand needs a fully-initialized container and bootstraps
        # everything synchronously.
        is_light = bootstrap_level(ctx.invoked_subcommand) == "light"
        defer_background = (ctx.invoked_subcommand is None or is_light) and not json_mode

        try:
            from velune.core.runtime import build_runtime

            runtime = build_runtime(
                workspace=workspace,
                config_path=config_path,
                verbose=verbose,
                defer_background=defer_background,
            )
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
                if not sys.stdin.isatty():
                    # Non-interactive (piped stdin, CI, cron): check providers and exit if none.
                    from velune.providers.keystore import list_configured_providers

                    if not list_configured_providers():
                        runtime.console.print(
                            Text.from_markup(
                                f"[{design.WARN}]No AI providers configured.[/{design.WARN}]  "
                                f"[{design.MUTED}]Run velune setup to configure a provider.[/{design.MUTED}]"
                            )
                        )
                        raise typer.Exit()
                else:
                    # Interactive: run state-machine onboarding.
                    from velune.cli.onboarding import (
                        onboarding_state,
                        run_onboarding,
                    )

                    state = onboarding_state()

                    if state == "returning":
                        # Advisory repo detection for returning users — hint only (Rule 12).
                        repo_name = _detect_repo_marker(workspace)
                        if repo_name:
                            logging.getLogger("velune").debug(
                                "Detected project marker before REPL launch: %s", repo_name
                            )
                    elif state == "partial":
                        # Providers configured but no model selected — jump to model discovery.
                        run_onboarding(runtime, skip_to="model_discovery")
                    else:
                        # Fresh install — run the full guided wizard.
                        run_onboarding(runtime, skip_to=None)

                _startup_mark("REPL handoff (prompt visible)")
                from velune.kernel.entrypoint import launch

                launch(runtime)

    register_commands(app, ServiceContainer(), only=register)
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
