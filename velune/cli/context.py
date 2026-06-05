"""CLI runtime context passed through Typer commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from velune.core.runtime import RuntimeContext
from velune.kernel.config import VeluneConfig
from velune.kernel.registry import ServiceContainer


@dataclass(slots=True)
class CLIContext:
    """Shared CLI state for the current process."""

    workspace: Path
    config_path: Path | None
    verbose: bool
    runtime: RuntimeContext
    json_mode: bool = False

    @property
    def console(self) -> Console:
        return self.runtime.console

    @property
    def config(self) -> VeluneConfig:
        return self.runtime.config

    @property
    def container(self) -> ServiceContainer:
        return self.runtime.container
