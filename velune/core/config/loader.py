"""Configuration loading from TOML files."""

import os
from pathlib import Path
from typing import Optional
import toml
from velune.core.config.schema import VeluneConfig


class ConfigLoader:
    """Loads and validates Velune configuration."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or self._find_config_path()

    def _find_config_path(self) -> Path:
        """Find the velune.toml configuration file."""
        current_dir = Path.cwd()
        
        while current_dir != current_dir.parent:
            config_file = current_dir / "velune.toml"
            if config_file.exists():
                return config_file
            current_dir = current_dir.parent
        
        # Fallback to home directory
        home_config = Path.home() / ".velune" / "velune.toml"
        if home_config.exists():
            return home_config
        
        raise FileNotFoundError("No velune.toml configuration found")

    def load(self) -> VeluneConfig:
        """Load and validate configuration."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
        data = toml.load(self.config_path)
        return VeluneConfig(**data)

    def load_with_env_overrides(self) -> VeluneConfig:
        """Load configuration with environment variable overrides."""
        config = self.load()
        
        # Override provider API keys from environment
        if config.providers.openai and config.providers.openai.api_key_env:
            api_key = os.getenv(config.providers.openai.api_key_env)
            if api_key:
                config.providers.openai.api_key_env = api_key
        
        if config.providers.anthropic and config.providers.anthropic.api_key_env:
            api_key = os.getenv(config.providers.anthropic.api_key_env)
            if api_key:
                config.providers.anthropic.api_key_env = api_key
        
        return config
