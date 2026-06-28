import os
from pathlib import Path
import pytest
import toml

from velune.kernel.config import (
    VeluneConfig,
    ConfigLoader,
    ConfigService,
    get_default_config,
    ConfigValidationError,
    _strip_defaults,
    _deep_merge
)

def test_default_config():
    config = get_default_config()
    assert config.project.name == "velune"
    assert config.providers.default_provider == "openai"
    assert config.execution.sandbox_enabled is True

def test_load_empty_config(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.touch()
    loader = ConfigLoader(config_file)
    config = loader.load()
    assert config.project.name == "velune"

def test_load_corrupted_config(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("invalid [ toml {")
    loader = ConfigLoader(config_file)
    config = loader.load()
    # It should fallback to default config
    assert config.project.name == "velune"
    assert config.execution.sandbox_enabled is True

def test_load_partial_config(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("""
[project]
name = "test_project"

[execution]
sandbox_enabled = false
""")
    loader = ConfigLoader(config_file)
    config = loader.load()
    assert config.project.name == "test_project"
    assert config.execution.sandbox_enabled is False
    # Check that others are default
    assert config.providers.default_provider == "openai"

def test_env_overrides(monkeypatch):
    monkeypatch.setenv("VELUNE_PROJECT__NAME", "env_project")
    monkeypatch.setenv("VELUNE_EXECUTION__SANDBOX_ENABLED", "False")
    
    config = get_default_config()
    assert config.project.name == "env_project"
    assert config.execution.sandbox_enabled is False

def test_env_overrides_with_toml(tmp_path, monkeypatch):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("""
[project]
name = "toml_project"
""")
    monkeypatch.setenv("VELUNE_EXECUTION__SANDBOX_ENABLED", "False")
    
    loader = ConfigLoader(config_file)
    # The TOML load sets project name from kwargs, taking precedence over env vars
    # Environment variables fill in the rest
    config = loader.load_with_env_overrides()
    assert config.project.name == "toml_project"
    assert config.execution.sandbox_enabled is False

def test_validate_healthy_config():
    config = VeluneConfig()
    errors = config.validate()
    # Should only fail on API key missing if it's strictly checking
    # Actually, default_provider is openai, api_key_env is OPENAI_API_KEY
    # If it's not set, it gives an error.
    if not os.getenv("OPENAI_API_KEY"):
        assert len(errors) == 1
        assert errors[0].field == "providers.openai.api_key_env"
        assert errors[0].severity == "CRITICAL"
    else:
        assert len(errors) == 0

def test_validate_missing_provider():
    config = VeluneConfig()
    config.providers.default_provider = "nonexistent_provider"
    errors = config.validate()
    assert len(errors) == 1
    assert errors[0].field == "providers.default_provider"

def test_validate_workspace_root_invalid(tmp_path):
    config = VeluneConfig()
    config.workspace.root = tmp_path / "does_not_exist"
    errors = config.validate()
    # Filter for workspace.root error
    ws_errors = [e for e in errors if e.field == "workspace.root"]
    assert len(ws_errors) == 1

def test_save_to_project(tmp_path):
    config = VeluneConfig()
    config.project.name = "saved_project"
    config.execution.sandbox_enabled = False
    
    saved_path = config.save_to_project(tmp_path)
    assert saved_path.exists()
    
    # Check contents
    data = toml.load(saved_path)
    assert data["project"]["name"] == "saved_project"
    assert data["execution"]["sandbox_enabled"] is False
    assert "memory" not in data  # Because it hasn't changed

def test_deep_merge():
    base = {"a": 1, "b": {"c": 2}}
    override = {"b": {"d": 3}, "e": 4}
    merged = _deep_merge(base, override)
    assert merged == {"a": 1, "b": {"c": 2, "d": 3}, "e": 4}

def test_strip_defaults():
    defaults = {"a": 1, "b": {"c": 2, "d": 3}}
    current = {"a": 1, "b": {"c": 2, "d": 4}, "e": 5}
    stripped = _strip_defaults(current, defaults)
    assert stripped == {"b": {"d": 4}, "e": 5}
