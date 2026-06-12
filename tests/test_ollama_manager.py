"""Tests for the OllamaManager and pull UI data layer."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.providers.ollama_manager import RECOMMENDED_MODELS, OllamaManager

# ── Static data tests ──────────────────────────────────────────────────────


def test_recommended_models_not_empty():
    assert len(RECOMMENDED_MODELS) >= 8


def test_recommended_models_have_required_fields():
    required = {"model_id", "size_gb", "description", "ram_needed", "skill"}
    for m in RECOMMENDED_MODELS:
        missing = required - m.keys()
        assert not missing, f"{m['model_id']} is missing fields: {missing}"


def test_nomic_embed_is_in_recommendations():
    ids = [m["model_id"] for m in RECOMMENDED_MODELS]
    assert "nomic-embed-text" in ids


def test_coding_models_exist():
    coding = [m for m in RECOMMENDED_MODELS if m["skill"] == "coding"]
    assert len(coding) >= 2


def test_all_models_have_positive_size():
    for m in RECOMMENDED_MODELS:
        assert m["size_gb"] >= 0, f"{m['model_id']} has invalid size"


def test_all_skills_are_known():
    valid_skills = {"coding", "reasoning", "embedding", "general"}
    for m in RECOMMENDED_MODELS:
        assert m["skill"] in valid_skills, f"{m['model_id']} has unknown skill: {m['skill']}"


def test_all_ram_needed_are_parseable():
    for m in RECOMMENDED_MODELS:
        val = m["ram_needed"].replace(" GB", "").strip()
        assert float(val) > 0, f"{m['model_id']} has invalid ram_needed"


# ── OllamaManager async tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_running_false_when_ollama_not_up():
    manager = OllamaManager()
    with patch.object(manager._client, "get", side_effect=Exception("connection refused")):
        result = await manager.is_running()
    assert result is False


@pytest.mark.asyncio
async def test_is_running_true_on_200():
    manager = OllamaManager()
    mock_response = MagicMock()
    mock_response.status_code = 200
    with patch.object(manager._client, "get", new=AsyncMock(return_value=mock_response)):
        result = await manager.is_running()
    assert result is True


@pytest.mark.asyncio
async def test_list_local_models_empty_on_error():
    manager = OllamaManager()
    with patch.object(manager._client, "get", side_effect=Exception("connection refused")):
        result = await manager.list_local_models()
    assert result == []


@pytest.mark.asyncio
async def test_list_local_models_parses_response():
    manager = OllamaManager()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "models": [{"name": "llama3.2:3b"}, {"name": "qwen2.5-coder:7b"}]
    }
    with patch.object(manager._client, "get", new=AsyncMock(return_value=mock_response)):
        result = await manager.list_local_models()
    assert "llama3.2:3b" in result
    assert "qwen2.5-coder:7b" in result


@pytest.mark.asyncio
async def test_delete_model_returns_false_on_error():
    manager = OllamaManager()
    with patch.object(manager._client, "delete", side_effect=Exception("refused")):
        result = await manager.delete_model("some-model")
    assert result is False


@pytest.mark.asyncio
async def test_delete_model_returns_true_on_200():
    manager = OllamaManager()
    mock_response = MagicMock()
    mock_response.status_code = 200
    with patch.object(manager._client, "delete", new=AsyncMock(return_value=mock_response)):
        result = await manager.delete_model("llama3.2:3b")
    assert result is True


@pytest.mark.asyncio
async def test_close_calls_aclose():
    manager = OllamaManager()
    manager._client.aclose = AsyncMock()
    await manager.close()
    manager._client.aclose.assert_called_once()
