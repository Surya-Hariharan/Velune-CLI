"""CLI command discovery and registration."""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType

import typer

from velune.cli.commands import (
    ask_command,
    chat_command,
    config_cmd,
    context_cmd,
    daemon_cmd,
    doctor_cmd,
    init_command,
    mcp_cmd,
    memory_cmd,
    models_cmd,
    retrieval_cmd,
    run_command,
    session_cmd,
    setup_command,
    trace_cmd,
    workspace_cmd,
)
from velune.kernel.registry import ServiceContainer

BUILTIN_COMMAND_MODULES: Sequence[str] = (
    "velune.cli.commands.ask",
    "velune.cli.commands.chat",
    "velune.cli.commands.run",
    "velune.cli.commands.models",
    "velune.cli.commands.workspace",
    "velune.cli.commands.session",
    "velune.cli.commands.memory",
    "velune.cli.commands.config",
    "velune.cli.commands.context",
    "velune.cli.commands.trace",
    "velune.cli.commands.retrieval",
    "velune.cli.commands.daemon",
    "velune.cli.commands.doctor",
    "velune.cli.commands.mcp",
)

_CORE = "Core"
_WORKSPACE = "Workspace & Sessions"
_SETUP = "Setup & Models"
_DIAG = "Diagnostics"


def discover_builtin_modules() -> list[ModuleType]:
    """Import and return built-in command modules."""
    modules: list[ModuleType] = []
    for module_name in BUILTIN_COMMAND_MODULES:
        modules.append(importlib.import_module(module_name))
    return modules


def discover_plugin_entry_points(group: str = "velune.commands") -> list[EntryPoint]:
    """Return third-party command extension entry points."""
    return list(entry_points(group=group))


def register_commands(app: typer.Typer, container: ServiceContainer) -> None:
    """Attach all built-in and plugin command groups to the app."""

    # ── Core — what you use every day ────────────────────────────────────
    app.command(name="chat", rich_help_panel=_CORE)(chat_command)
    app.command(name="run", rich_help_panel=_CORE)(run_command)
    app.command(name="ask", rich_help_panel=_CORE)(ask_command)
    app.command(name="init", rich_help_panel=_CORE)(init_command)

    # ── Workspace & Sessions ──────────────────────────────────────────────
    app.add_typer(
        workspace_cmd,
        name="workspace",
        help="Browse, index, and switch projects.",
        rich_help_panel=_WORKSPACE,
    )
    app.add_typer(
        session_cmd,
        name="session",
        help="List, resume, or delete chat sessions.",
        rich_help_panel=_WORKSPACE,
    )

    # ── Setup & Models ────────────────────────────────────────────────────
    app.command(name="setup", rich_help_panel=_SETUP)(setup_command)
    app.add_typer(
        models_cmd,
        name="models",
        help="Scan, list, and assign AI models.",
        rich_help_panel=_SETUP,
    )
    app.add_typer(
        config_cmd,
        name="config",
        help="Read and write velune.toml settings.",
        rich_help_panel=_SETUP,
    )

    # ── Diagnostics ───────────────────────────────────────────────────────
    app.add_typer(
        doctor_cmd,
        name="doctor",
        help="Check that providers, models, and paths are healthy.",
        rich_help_panel=_DIAG,
    )
    # `trace` is registered as `logs` — easier to remember, same behaviour.
    app.add_typer(
        trace_cmd,
        name="logs",
        help="View recent execution events (alias: velune logs live to follow).",
        rich_help_panel=_DIAG,
    )
    app.add_typer(
        daemon_cmd,
        name="daemon",
        help="Start, stop, or check the background Velune service.",
        rich_help_panel=_DIAG,
    )
    app.add_typer(
        mcp_cmd,
        name="mcp",
        help="Connect to or expose an MCP server.",
        rich_help_panel=_DIAG,
    )
    app.add_typer(
        memory_cmd,
        name="memory",
        help="Inspect, clear, or compact AI memory tiers.",
        rich_help_panel=_DIAG,
    )
    # `context` is registered as `status` — shows index freshness and workspace health.
    app.add_typer(
        context_cmd,
        name="status",
        help="Show index freshness, file counts, and workspace health.",
        rich_help_panel=_DIAG,
    )
    # `retrieval` is a developer diagnostic; registered last in the group.
    app.add_typer(
        retrieval_cmd,
        name="pipeline",
        help="Trace a retrieval query through the search pipeline.",
        rich_help_panel=_DIAG,
    )

    for entry_point in discover_plugin_entry_points():
        loaded = entry_point.load()
        if hasattr(loaded, "register"):
            loaded.register(app=app, container=container)
        elif callable(loaded):
            loaded(app=app, container=container)
