"""CLI command discovery and registration.

Commands are described as a pure-data **spec table** and imported lazily. This
keeps the common entry paths fast:

* ``velune`` (no args, the REPL) and ``velune --help`` import **zero** command
  modules — the REPL needs none, and top-level help is rendered directly from
  the spec table.
* ``velune <subcommand> ...`` imports **only** that subcommand's module.

Importing every command module eagerly (the previous behaviour) cost ~1.5s on
every invocation because their transitive dependencies are heavy. The spec
table removes that from the hot path while keeping help output and the command
set in perfect sync — both derive from the same specs.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer

    from velune.kernel.registry import ServiceContainer


_CORE = "Core"
_WORKSPACE = "Workspace & Sessions"
_SETUP = "Setup & Models"
_ANALYTICS = "Analytics & Monitoring"
_DIAG = "Diagnostics"
_RECOVERY = "Trust & Recovery"


@dataclass(frozen=True)
class CommandSpec:
    """Declarative description of one top-level CLI command or group.

    ``module``/``attr`` are imported only when this command is registered, so a
    spec costs nothing until its command is actually invoked.
    """

    name: str
    kind: str  # "command" (a function) | "typer" (a sub-app group)
    module: str
    attr: str
    panel: str
    help: str
    hidden: bool = False
    # How much of the runtime this command needs at dispatch:
    #   "full"  — Tier-0 + Tier-1 bootstrapped synchronously (default; required
    #             by commands that read memory/retrieval/cognition/orchestration).
    #   "light" — Tier-0 only; the expensive Tier-1 subsystems are skipped
    #             entirely. For read-only/diagnostic commands that consume just
    #             config + providers/models/console. Cuts ~2.2s off startup.
    bootstrap: str = "full"


# Single source of truth for the built-in command tree. Order within a panel is
# preserved in help output.
COMMAND_SPECS: tuple[CommandSpec, ...] = (
    # ── Core — what you use every day ────────────────────────────────────
    CommandSpec(
        "chat",
        "command",
        "velune.cli.commands.chat",
        "chat_command",
        _CORE,
        "Start an interactive chat session.",
    ),
    CommandSpec(
        "run",
        "command",
        "velune.cli.commands.run",
        "run_command",
        _CORE,
        "Run an autonomous, multi-step task.",
    ),
    CommandSpec(
        "ask",
        "command",
        "velune.cli.commands.ask",
        "ask_command",
        _CORE,
        "Ask a single one-off question.",
    ),
    CommandSpec(
        "init",
        "command",
        "velune.cli.commands.init",
        "init_command",
        _CORE,
        "Initialize Velune in the current workspace.",
    ),
    CommandSpec(
        "onboard",
        "command",
        "velune.cli.commands.onboard",
        "onboard_command",
        _CORE,
        "Run the first-time setup wizard, or resume an incomplete run.",
        bootstrap="light",
    ),
    # ── Workspace & Sessions ──────────────────────────────────────────────
    CommandSpec(
        "project",
        "typer",
        "velune.cli.commands.workspace",
        "workspace_cmd",
        _WORKSPACE,
        "Open, index, and switch projects.",
    ),
    CommandSpec(
        "workspace",
        "typer",
        "velune.cli.commands.workspace",
        "workspace_cmd",
        _WORKSPACE,
        "Alias of `project`.",
        hidden=True,
    ),
    CommandSpec(
        "session",
        "typer",
        "velune.cli.commands.session",
        "session_cmd",
        _WORKSPACE,
        "List, resume, or delete chat sessions.",
    ),
    # ── Setup & Models ────────────────────────────────────────────────────
    CommandSpec(
        "setup",
        "command",
        "velune.cli.commands.setup",
        "setup_command",
        _SETUP,
        "Configure providers and models interactively.",
    ),
    CommandSpec(
        "models",
        "typer",
        "velune.cli.commands.models",
        "models_cmd",
        _SETUP,
        "Scan, list, and assign AI models.",
    ),
    CommandSpec(
        "provider",
        "typer",
        "velune.cli.commands.providers",
        "provider_cmd",
        _SETUP,
        "Manage AI providers — add, remove, test, list, and check status.",
    ),
    CommandSpec(
        "config",
        "typer",
        "velune.cli.commands.config",
        "config_cmd",
        _SETUP,
        "Read and write velune.toml settings.",
        bootstrap="light",
    ),
    CommandSpec(
        "trust",
        "typer",
        "velune.cli.commands.trust",
        "trust_cmd",
        _SETUP,
        "Trust, list, or revoke workspace directories.",
        bootstrap="light",
    ),
    # ── Analytics & Monitoring ────────────────────────────────────────────
    CommandSpec(
        "usage",
        "command",
        "velune.cli.commands.usage",
        "usage_command",
        _ANALYTICS,
        "Show token usage and cost.",
        bootstrap="light",
    ),
    CommandSpec(
        "quota",
        "command",
        "velune.cli.commands.usage",
        "quota_command",
        _ANALYTICS,
        "Show provider quota status.",
        bootstrap="light",
    ),
    CommandSpec(
        "health",
        "command",
        "velune.cli.commands.usage",
        "health_command",
        _ANALYTICS,
        "Show provider health.",
        bootstrap="light",
    ),
    # ── Diagnostics ───────────────────────────────────────────────────────
    CommandSpec(
        "doctor",
        "typer",
        "velune.cli.commands.doctor",
        "doctor_cmd",
        _DIAG,
        "Check that providers, models, and paths are healthy.",
        bootstrap="light",
    ),
    CommandSpec(
        "logs",
        "typer",
        "velune.cli.commands.trace",
        "trace_cmd",
        _DIAG,
        "View recent execution events (alias of `trace`).",
        bootstrap="light",
    ),
    CommandSpec(
        "daemon",
        "typer",
        "velune.cli.commands.daemon",
        "daemon_cmd",
        _DIAG,
        "Start, stop, or check the background Velune service.",
    ),
    CommandSpec(
        "mcp",
        "typer",
        "velune.cli.commands.mcp",
        "mcp_cmd",
        _DIAG,
        "Connect to or expose an MCP server.",
    ),
    CommandSpec(
        "memory",
        "typer",
        "velune.cli.commands.memory",
        "memory_cmd",
        _DIAG,
        "Inspect, clear, or compact AI memory tiers.",
    ),
    CommandSpec(
        "status",
        "typer",
        "velune.cli.commands.context",
        "context_cmd",
        _DIAG,
        "Show index freshness, file counts, and workspace health.",
        bootstrap="light",
    ),
    CommandSpec(
        "pipeline",
        "typer",
        "velune.cli.commands.retrieval",
        "retrieval_cmd",
        _DIAG,
        "Trace a retrieval query through the search pipeline.",
    ),
    # ── Trust & Recovery — never lose work ───────────────────────────────
    CommandSpec(
        "backup",
        "command",
        "velune.cli.commands.backup",
        "backup_cmd",
        _RECOVERY,
        "Snapshot all Velune state to one portable archive.",
        bootstrap="light",
    ),
    CommandSpec(
        "restore",
        "command",
        "velune.cli.commands.backup",
        "restore_cmd",
        _RECOVERY,
        "Restore Velune state from a backup archive.",
        bootstrap="light",
    ),
    CommandSpec(
        "recover",
        "command",
        "velune.cli.commands.recover",
        "recover_cmd",
        _RECOVERY,
        "Recover an unsaved session left by a crash.",
        bootstrap="light",
    ),
)

_SPECS_BY_NAME: dict[str, CommandSpec] = {spec.name: spec for spec in COMMAND_SPECS}


def bootstrap_level(command: str | None) -> str:
    """Return the runtime bootstrap level for an invoked subcommand.

    ``"light"`` for read-only/diagnostic commands that need only Tier-0, else
    ``"full"``. Unknown commands (and ``None``) default to ``"full"`` so a
    misclassification can never silently starve a command of a subsystem.
    """
    spec = _SPECS_BY_NAME.get(command) if command else None
    return spec.bootstrap if spec is not None else "full"


PANEL_ORDER: tuple[str, ...] = (_CORE, _WORKSPACE, _SETUP, _ANALYTICS, _DIAG, _RECOVERY)

BUILTIN_COMMAND_MODULES: Sequence[str] = tuple(dict.fromkeys(spec.module for spec in COMMAND_SPECS))


def discover_builtin_modules() -> list[ModuleType]:
    """Import and return built-in command modules (eager — diagnostics only)."""
    return [importlib.import_module(name) for name in BUILTIN_COMMAND_MODULES]


def discover_plugin_entry_points(group: str = "velune.commands") -> list[EntryPoint]:
    """Return third-party command extension entry points."""
    return list(entry_points(group=group))


def _attach_spec(app: typer.Typer, spec: CommandSpec) -> None:
    """Lazily import *spec*'s module and attach it to *app*."""
    module = importlib.import_module(spec.module)
    target = getattr(module, spec.attr)
    if spec.kind == "command":
        app.command(name=spec.name, rich_help_panel=spec.panel, hidden=spec.hidden)(target)
    else:  # "typer" sub-app group
        app.add_typer(
            target,
            name=spec.name,
            help=spec.help,
            rich_help_panel=spec.panel,
            hidden=spec.hidden,
        )


def _attach_plugins(app: typer.Typer, container: ServiceContainer) -> None:
    for entry_point in discover_plugin_entry_points():
        loaded = entry_point.load()
        if hasattr(loaded, "register"):
            loaded.register(app=app, container=container)
        elif callable(loaded):
            loaded(app=app, container=container)


def register_commands(
    app: typer.Typer,
    container: ServiceContainer,
    only: str | None = "__all__",
) -> None:
    """Attach built-in (and plugin) command groups to *app*.

    ``only`` controls how much is imported:

    * ``"__all__"`` (default) — register every command + plugins. Used for the
      app singleton, shell completion, and tests.
    * ``None`` — register nothing. Used for the bare REPL launch, which needs no
      subcommands and should import no command modules.
    * a command name — register just that one built-in command (lazy import of a
      single module). Unknown names fall back to registering everything so Typer
      can still produce a proper "no such command" error with suggestions.
    """
    if only is None:
        return

    if only != "__all__":
        spec = _SPECS_BY_NAME.get(only)
        if spec is not None:
            _attach_spec(app, spec)
            return
        # Unknown name (possibly a plugin or a typo): fall through to full
        # registration so Typer resolves or errors correctly.

    for spec in COMMAND_SPECS:
        _attach_spec(app, spec)
    _attach_plugins(app, container)


def iter_specs(include_hidden: bool = False) -> Iterable[CommandSpec]:
    """Yield command specs in panel order (built-ins only)."""
    for panel in PANEL_ORDER:
        for spec in COMMAND_SPECS:
            if spec.panel == panel and (include_hidden or not spec.hidden):
                yield spec


def render_root_help() -> None:
    """Print the top-level CLI help from the spec table — imports no commands.

    Mirrors Typer's grouped layout closely enough to be a drop-in for
    ``velune --help`` while staying instant.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from velune import __version__
    from velune.cli import design

    console = Console(highlight=False)
    console.print(
        Panel(
            f"[bold {design.ACCENT}]velune[/bold {design.ACCENT}] "
            f"[{design.MUTED}]v{__version__}[/{design.MUTED}]\n"
            "[dim]Terminal-first cognitive AI orchestration system[/dim]\n\n"
            "[dim]Run [bold]velune[/bold] with no arguments to start the interactive session.[/dim]",
            border_style=design.GREEN,
            padding=(0, 2),
        )
    )
    console.print(
        "\n[bold]Usage:[/bold] velune [OPTIONS] COMMAND [ARGS]...\n"
        "[bold]Options:[/bold] "
        "[cyan]--workspace/-w[/cyan]  [cyan]--config/-c[/cyan]  "
        "[cyan]--verbose/-v[/cyan]  [cyan]--version[/cyan]  [cyan]--help/-h[/cyan]\n"
    )
    for panel in PANEL_ORDER:
        rows = [s for s in COMMAND_SPECS if s.panel == panel and not s.hidden]
        if not rows:
            continue
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        table.add_column(style=f"bold {design.ACCENT}", no_wrap=True)
        table.add_column(style="dim")
        for spec in rows:
            table.add_row(spec.name, spec.help)
        console.print(f"[bold {design.INFO}]{panel}[/bold {design.INFO}]")
        console.print(table)
        console.print()
    console.print("[dim]Run [bold]velune COMMAND --help[/bold] for details on a command.[/dim]")
