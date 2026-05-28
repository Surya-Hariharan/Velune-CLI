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
    daemon_cmd,
    doctor_cmd,
    mcp_cmd,
    mcp_serve,
    memory_cmd,
    models_cmd,
    run_command,
    workspace_cmd,
)
from velune.kernel.registry import ServiceContainer

BUILTIN_COMMAND_MODULES: Sequence[str] = (
    "velune.cli.commands.ask",
    "velune.cli.commands.chat",
    "velune.cli.commands.run",
    "velune.cli.commands.models",
    "velune.cli.commands.workspace",
    "velune.cli.commands.memory",
    "velune.cli.commands.config",
    "velune.cli.commands.daemon",
    "velune.cli.commands.doctor",
    "velune.cli.commands.mcp",
)


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

    app.command(name="ask")(ask_command)
    app.command(name="chat")(chat_command)
    app.command(name="run")(run_command)
    app.command(name="mcp-serve")(mcp_serve)
    app.add_typer(models_cmd, name="models")
    app.add_typer(workspace_cmd, name="workspace")
    app.add_typer(memory_cmd, name="memory")
    app.add_typer(config_cmd, name="config")
    app.add_typer(daemon_cmd, name="daemon")
    app.add_typer(doctor_cmd, name="doctor")
    app.add_typer(mcp_cmd, name="mcp")


    for entry_point in discover_plugin_entry_points():
        loaded = entry_point.load()
        if hasattr(loaded, "register"):
            loaded.register(app=app, container=container)
        elif callable(loaded):
            loaded(app=app, container=container)
