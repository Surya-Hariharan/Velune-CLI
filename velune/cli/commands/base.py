"""Command registration contracts."""

from __future__ import annotations

from typing import Protocol

import typer

from velune.core.registry.container import ServiceContainer


class CommandRegistrar(Protocol):
    """A command module that can attach itself to a Typer app."""

    def register(self, app: typer.Typer, container: ServiceContainer) -> None:
        """Register the command group or command callbacks."""
