import toml

from velune.kernel.config import (
    ConfigLoader,
    VeluneConfig,
    _deep_merge,
    _strip_defaults,
    get_default_config,
)


def test_default_config():
    config = get_default_config()
    assert config.project.name == "velune"
    assert config.providers.default_provider == "openai"
    assert config.execution.native_tools is True


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
    assert config.execution.native_tools is True


def test_load_partial_config(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("""
[project]
name = "test_project"

[execution]
native_tools = false
""")
    loader = ConfigLoader(config_file)
    config = loader.load()
    assert config.project.name == "test_project"
    assert config.execution.native_tools is False
    # Check that others are default
    assert config.providers.default_provider == "openai"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("VELUNE_PROJECT__NAME", "env_project")
    monkeypatch.setenv("VELUNE_EXECUTION__NATIVE_TOOLS", "False")

    config = get_default_config()
    assert config.project.name == "env_project"
    assert config.execution.native_tools is False


def test_env_overrides_with_toml(tmp_path, monkeypatch):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("""
[project]
name = "toml_project"
""")
    monkeypatch.setenv("VELUNE_EXECUTION__NATIVE_TOOLS", "False")

    loader = ConfigLoader(config_file)
    # The TOML load sets project name from kwargs, taking precedence over env vars
    # Environment variables fill in the rest
    config = loader.load_with_env_overrides()
    assert config.project.name == "toml_project"
    assert config.execution.native_tools is False


def test_validate_healthy_config():
    # The defaults are valid. No API key is required: keys live in the keystore.
    assert VeluneConfig().validate() == []


def test_validate_accepts_every_catalog_provider():
    config = VeluneConfig()
    for name in ("groq", "google", "openai-compat", "openai_compat", "ollama"):
        config.providers.default_provider = name
        assert config.validate() == [], name


def test_validate_missing_provider():
    config = VeluneConfig()
    config.providers.default_provider = "nonexistent_provider"
    errors = config.validate()
    assert len(errors) == 1
    assert errors[0].field == "providers.default_provider"
    assert errors[0].severity == "WARNING"


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
    config.execution.native_tools = False

    saved_path = config.save_to_project(tmp_path)
    assert saved_path.exists()

    # Check contents
    data = toml.load(saved_path)
    assert data["project"]["name"] == "saved_project"
    assert data["execution"]["native_tools"] is False
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


async def test_startup_logs_config_problems_but_keeps_going(caplog):
    from velune.kernel.lifecycle import LifecycleCoordinator

    config = VeluneConfig()
    config.providers.default_provider = "nonexistent_provider"
    lifecycle = LifecycleCoordinator()
    lifecycle.set_config(config)
    with caplog.at_level("WARNING", logger="velune.kernel.lifecycle"):
        await lifecycle.startup()  # must not raise
    assert "nonexistent_provider" in caplog.text


def test_build_runtime_attaches_the_config_for_validation(tmp_path):
    from velune.core.runtime import build_runtime

    runtime = build_runtime(tmp_path, defer_background=True)
    lifecycle = runtime.container.get("runtime.lifecycle")
    assert lifecycle._config is runtime.config
