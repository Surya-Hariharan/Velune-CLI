"""Tests for configuration loading and override behaviour."""

from pathlib import Path

import toml

from velune.kernel.config import ConfigLoader, VeluneConfig
from velune.repository.scanner import DEFAULT_VELUNEIGNORE, FilesystemScanner


def test_default_config_loads():
    config = VeluneConfig()
    assert config is not None
    assert config.providers.default_provider == "openai"
    assert config.telemetry.log_level == "INFO"


def test_project_config_overrides_global(tmp_path: Path):
    global_toml = tmp_path / "global.toml"
    global_toml.write_text(toml.dumps({"providers": {"default_provider": "openai"}}))

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_toml = project_dir / "velune.toml"
    project_toml.write_text(toml.dumps({"providers": {"default_provider": "anthropic"}}))

    global_config = ConfigLoader(config_path=global_toml).load()
    project_config = ConfigLoader(config_path=project_toml).load()

    assert global_config.providers.default_provider == "openai"
    assert project_config.providers.default_provider == "anthropic"
    # Values absent from the project file still use defaults
    assert project_config.telemetry.log_level == "INFO"
    assert project_config.execution.sandbox_enabled is True


def test_veluneignore_template_is_valid(tmp_path: Path):
    (tmp_path / ".veluneignore").write_text(DEFAULT_VELUNEIGNORE)
    scanner = FilesystemScanner(tmp_path)
    patterns = scanner.gitignore_patterns

    assert ".env" in patterns
    assert "*.pem" in patterns
    assert "*.key" in patterns
    assert "*.pyc" in patterns
