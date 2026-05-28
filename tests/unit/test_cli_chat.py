import pytest
from unittest.mock import MagicMock, AsyncMock, patch
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
    config.telemetry.enabled = False
    runtime.config = config

    # Mock container
    container = MagicMock()
    container.has.return_value = True

    # Mock services
    lifecycle = MagicMock()
    lifecycle.startup = AsyncMock()
    lifecycle.shutdown = AsyncMock()

    model_registry = MagicMock()
    model_registry.refresh = AsyncMock()

    orchestrator = MagicMock()
    model_specialization = MagicMock()
    
    from velune.models.specializations import CouncilRole
    coder_model = MagicMock()
    coder_model.model_id = "test-coder-model"
    coder_model.provider_id = "test-provider"
    
    model_specialization.map_roles.return_value = {
        CouncilRole.CODER: coder_model
    }
    orchestrator.mapper = model_specialization

    repo_cognition = MagicMock()
    snapshot = MagicMock()
    snapshot.root_path = "/tmp"
    snapshot.files = []
    repo_cognition.index.return_value = snapshot

    provider_registry = MagicMock()
    provider = MagicMock()
    provider.get_capabilities.return_value = MagicMock(supports_streaming=True)
    provider.stream = AsyncMock()
    provider_registry.get_or_raise.return_value = provider

    container.get.side_effect = lambda key: {
        "runtime.lifecycle": lifecycle,
        "runtime.model_registry": model_registry,
        "runtime.council_orchestrator": orchestrator,
        "runtime.repository_cognition": repo_cognition,
        "runtime.provider_registry": provider_registry,
        "runtime.config": config,
    }.get(key, MagicMock())

    runtime.container = container
    return runtime


def test_cli_chat_exit_immediately(mock_runtime):
    """Verify that chat command starts up, prompts, and exits cleanly when user inputs '!exit'."""
    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        with patch("velune.cli.commands.preflight.run_preflight_check", return_value=AsyncMock(return_value=True)()):
            # Mock console.input to return "!exit" on first call
            with patch("rich.console.Console.input", return_value="!exit"):
                result = runner.invoke(app, ["chat"])
                assert result.exit_code == 0
                
                # Check that lifecycle startup and shutdown were called
                mock_runtime.container.get("runtime.lifecycle").startup.assert_called_once()
                mock_runtime.container.get("runtime.lifecycle").shutdown.assert_called_once()


def test_cli_chat_sends_message_and_streams(mock_runtime):
    """Verify that chat command streams assistant response when user provides input and then exits."""
    # We want input to return "hello" on first call, and then "!exit" on second call
    input_values = ["hello", "!exit"]
    
    def mock_input(*args, **kwargs):
        if not input_values:
            return "!exit"
        return input_values.pop(0)

    # Mock chunk for stream
    from velune.core.types.inference import StreamChunk
    mock_chunk = StreamChunk(content="Hello! I am your AI assistant.")
    
    async def mock_stream(*args, **kwargs):
        yield mock_chunk

    provider = mock_runtime.container.get("runtime.provider_registry").get_or_raise()
    provider.stream.side_effect = mock_stream

    with patch("velune.cli.app.build_runtime", return_value=mock_runtime):
        with patch("velune.cli.commands.preflight.run_preflight_check", return_value=AsyncMock(return_value=True)()):
            with patch("rich.console.Console.input", side_effect=mock_input):
                result = runner.invoke(app, ["chat"])
                assert result.exit_code == 0
                
                # Verify that provider stream was called once for "hello"
                provider.stream.assert_called_once()
