"""CLI command discovery and registration."""

from __future__ import annotations

import importlib
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType
from typing import Iterable, Sequence

import typer

from velune.cli.commands import ask_cmd, config_cmd, memory_cmd, models_cmd, run_cmd, workspace_cmd
from velune.core.registry.container import ServiceContainer

BUILTIN_COMMAND_MODULES: Sequence[str] = (
    "velune.cli.commands.ask",
    "velune.cli.commands.run",
    "velune.cli.commands.models",
    "velune.cli.commands.workspace",
    "velune.cli.commands.memory",
    "velune.cli.commands.config",
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

    app.add_typer(ask_cmd, name="ask")
    app.command()(run_cmd)
    app.add_typer(models_cmd, name="models")
    app.add_typer(workspace_cmd, name="workspace")
    app.add_typer(memory_cmd, name="memory")
    app.add_typer(config_cmd, name="config")

    for entry_point in discover_plugin_entry_points():
        loaded = entry_point.load()
        if hasattr(loaded, "register"):
            loaded.register(app=app, container=container)
        elif callable(loaded):
            loaded(app=app, container=container)