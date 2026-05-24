"""CLI runtime context passed through Typer commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from velune.kernel.config import VeluneConfig
from velune.kernel.registry import ServiceContainer
from velune.core.runtime import RuntimeContext


@dataclass(slots=True)
class CLIContext:
    """Shared CLI state for the current process."""

    workspace: Path
    config_path: Optional[Path]
    verbose: bool
    runtime: RuntimeContext

    @property
    def console(self) -> Console:
        return self.runtime.console

    @property
    def config(self) -> VeluneConfig:
        return self.runtime.config

    @property
    def container(self) -> ServiceContainer:
        return self.runtime.container


@dataclass
class DaemonCLIContext:
    """Thin context that routes commands via daemon client."""
    client: Any
    workspace: Path
    config_path: Optional[Path] = None
    verbose: bool = False

    @property
    def console(self) -> Console:
        from rich.console import Console
        return Console()