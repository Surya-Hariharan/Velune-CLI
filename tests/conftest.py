"""Shared test fixtures for Velune test suite."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.kernel.config import ProvidersConfig, VeluneConfig


class MockModelProvider:
    """Test double for ModelProvider protocol."""

    provider_id = "mock"

    def __init__(self, response_content: str = '{"result": "ok"}'):
        self.response_content = response_content
        self.call_count = 0
        self.last_request: InferenceRequest | None = None

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.call_count += 1
        self.last_request = request
        return InferenceResponse(
            content=self.response_content,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=50,
            latency_ms=10.0,
        )

    async def stream(self, request: InferenceRequest):
        yield StreamChunk(content=self.response_content, finish_reason="stop")

    async def embed(self, texts, model_id):
        return [[0.1] * 1536 for _ in texts]

    async def health_check(self):
        return ProviderHealth.HEALTHY

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def get_capabilities(self):
        return ProviderCapabilities(
            supports_streaming=True,
            supports_embeddings=True,
        )

    async def list_models(self):
        return []


@pytest.fixture
def mock_provider() -> MockModelProvider:
    return MockModelProvider()


@pytest.fixture
def mock_provider_with_json() -> MockModelProvider:
    """Provider that returns valid JSON responses."""
    return MockModelProvider(response_content='{"facts": ["fact1"], "relations": []}')


@pytest.fixture
def temp_workspace(tmp_path: Path):
    """Temporary directory with a basic Python project structure."""
    import subprocess

    workspace = tmp_path / "test_workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "__init__.py").write_text("")
    (workspace / "main.py").write_text("def hello(): return 'world'\n")

    subprocess.run(["git", "init", str(workspace)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=workspace, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=workspace, capture_output=True)

    yield workspace


@pytest.fixture
def mock_config() -> VeluneConfig:
    """VeluneConfig with ollama as the default provider."""
    return VeluneConfig(providers=ProvidersConfig(default_provider="ollama"))


@pytest.fixture
def temp_velune_dir(tmp_path: Path) -> Path:
    """Create a .velune directory for testing."""
    velune_dir = tmp_path / ".velune"
    velune_dir.mkdir()
    return velune_dir


@pytest.fixture
def sqlite_manager(tmp_path: Path):
    """SQLiteManager backed by a temp file."""
    from velune.memory.storage.sqlite_manager import SQLiteManager

    db_path = tmp_path / "test.db"
    manager = SQLiteManager(db_path)
    yield manager
    manager._is_running = False


@pytest.fixture
def mock_model_descriptor() -> ModelDescriptor:
    return ModelDescriptor(
        model_id="test-model",
        provider_id="mock",
        display_name="Test Model",
        context_length=8192,
        capabilities=ModelCapabilityProfile(
            coding=CapabilityLevel.ADVANCED,
            reasoning=CapabilityLevel.INTERMEDIATE,
        ),
    )
