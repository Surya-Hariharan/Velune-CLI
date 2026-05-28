import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from typer.testing import CliRunner

from velune.cli.app import app
from velune.core.runtime import RuntimeContext

runner = CliRunner()


@pytest.fixture
def mock_runtime():
    runtime = MagicMock(spec=RuntimeContext)
    runtime.console = MagicMock()
    
    # Mock config
    config = MagicMock()
    config.project.name = "TestProject"
    config.project.version = "1.0"
    config.providers.default_provider = "test_provider"
    config.workspace.index_on_init = True
    config.workspace.watch_files = False
    config.workspace.git_aware = True
    config.telemetry.enabled = False
    config.telemetry.log_level = "info"
    
    config.memory.working_memory_ttl = 100
    config.memory.episodic_retention_days = 7
    config.memory.semantic_threshold = 0.8
    config.memory.graph_enabled = True
    
    runtime.config = config
    
    # Mock container
    container = MagicMock()
    # has/get behavior
    container.has.return_value = True
    
    # Mock services
    lifecycle = MagicMock()
    lifecycle.startup = AsyncMock()
    lifecycle.shutdown = AsyncMock()
    
    container.get.side_effect = lambda key: {
        "runtime.lifecycle": lifecycle,
        "runtime.config": config,
    }.get(key, MagicMock())
    
    runtime.container = container
    return runtime


def test_cli_version_json(mock_runtime):
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json", "--version"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "version" in data


def test_cli_ready_json(mock_runtime):
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ready"
        assert "workspace" in data


def test_cli_config_show_json(mock_runtime):
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json", "config", "show"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["project"]["name"] == "TestProject"
        assert data["providers"]["default"] == "test_provider"


def test_cli_config_get_json(mock_runtime):
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json", "config", "get", "project.name"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["key"] == "project.name"
        assert data["value"] == "TestProject"


def test_cli_config_set_json(mock_runtime, tmp_path):
    # Setup a mock config path
    mock_runtime.config_path = tmp_path / "velune.toml"
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        with patch("toml.dump") as mock_dump:
            result = runner.invoke(app, ["--json", "config", "set", "project.name", "NewProject"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["success"] is True
            assert data["key"] == "project.name"
            assert data["value"] == "NewProject"


def test_cli_workspace_status_json(mock_runtime, tmp_path):
    # Mock workspace .velune index folder existence
    (tmp_path / ".velune" / "index").mkdir(parents=True)
    
    # Mock repository_cognition
    repo_cognition = MagicMock()
    snapshot = MagicMock()
    snapshot.files = [MagicMock()]
    snapshot.symbols = [MagicMock(), MagicMock()]
    snapshot.summary = {"git": {"active_branch": "test-branch"}}
    repo_cognition.index.return_value = snapshot
    
    lifecycle = AsyncMock()
    
    mock_runtime.container.get.side_effect = lambda key: {
        "runtime.repository_cognition": repo_cognition,
        "runtime.lifecycle": lifecycle,
        "runtime.config": mock_runtime.config,
    }.get(key, MagicMock())
    
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json", "workspace", "status", "--path", str(tmp_path)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["workspace_root"] == str(tmp_path)
        assert data["indexed_files_count"] == 1
        assert data["indexed_symbols_count"] == 2
        assert data["git_branch"] == "test-branch"
        assert data["status"] == "Active & Fully Primed"


def test_cli_models_list_json(mock_runtime):
    # Mock model registry
    model_registry = MagicMock()
    record = MagicMock()
    record.model_id = "test-model"
    record.display_name = "Test Model"
    record.provider_id = "test-provider"
    
    from velune.core.types.model import CapabilityLevel
    caps = MagicMock()
    caps.coding = CapabilityLevel.BASIC
    caps.reasoning = CapabilityLevel.NONE
    caps.planning = CapabilityLevel.NONE
    caps.summarization = CapabilityLevel.NONE
    caps.tool_use = CapabilityLevel.NONE
    caps.long_context = CapabilityLevel.NONE
    record.capabilities = caps
    
    model_registry.list_all.return_value = [record]
    
    mock_runtime.container.get.side_effect = lambda key: {
        "runtime.model_registry": model_registry,
        "runtime.config": mock_runtime.config,
    }.get(key, MagicMock())
    
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json", "models", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["model_id"] == "test-model"
        assert "coding" in data[0]["capabilities"]
        assert "reasoning" not in data[0]["capabilities"]


def test_cli_memory_stats_json(mock_runtime):
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        result = runner.invoke(app, ["--json", "memory", "stats"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["working_memory_ttl"] == 100
        assert data["episodic_retention_days"] == 7
