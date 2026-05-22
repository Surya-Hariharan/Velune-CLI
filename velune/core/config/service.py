"""Configuration service helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from velune.core.config.defaults import get_default_config
from velune.core.config.loader import ConfigLoader
from velune.core.config.schema import VeluneConfig


@dataclass(slots=True)
class ConfigService:
    """Workspace-aware configuration service."""

    workspace: Path
    config_path: Optional[Path] = None

    def load(self) -> VeluneConfig:
        """Load configuration from the resolved workspace path."""

        config_path = self._resolve_config_path()
        try:
            return ConfigLoader(config_path).load_with_env_overrides()
        except FileNotFoundError:
            return get_default_config()

    def _resolve_config_path(self) -> Optional[Path]:
        if self.config_path:
            return self.config_path

        workspace_config = self.workspace / "velune.toml"
        if workspace_config.exists():
            return workspace_config

        return None