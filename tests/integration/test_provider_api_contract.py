"""Integration tests for Provider API contracts (Batch 13)."""

import pytest
from unittest.mock import MagicMock

from velune.memory.consolidator import MemoryConsolidator
from velune.context.compressor import ContextCompressor
from velune.intent.reconstructor import IntentReconstructor
from velune.memory.tiers.episodic import EpisodicTurn, EpisodicStep
from velune.intent.hypothesis import IntentHypothesis


@pytest.mark.asyncio
async def test_memory_consolidator_with_mock_provider_no_attribute_error(mock_provider_with_json) -> None:
    """Verify that MemoryConsolidator consolidates turns to semantic fact JSON with the mock provider."""
    # 1. Setup mocked tiers
    working = MagicMock()
    episodic = MagicMock()
    semantic = MagicMock()
    graph = MagicMock()
    archive = MagicMock()
    
    # Configure episodic to return sample turns and steps
    episodic.get_turns.return_value = [
        EpisodicTurn(session_id="session1", role="user", content="Add retry to main.py", timestamp=100.0),
        EpisodicTurn(session_id="session1", role="assistant", content="Sure, retry logic added.", timestamp=101.0)
    ]
    episodic.get_execution_steps.return_value = [
        EpisodicStep(session_id="session1", step_name="refactor", status="completed", timestamp=102.0)
    ]
    
    consolidator = MemoryConsolidator(
        working_tier=working,
        episodic_tier=episodic,
        semantic_tier=semantic,
        graph_tier=graph,
        archive_tier=archive
    )
    
    # 2. Configure mock provider to return facts and relations to exercise graph additions
    mock_provider_with_json.response_content = (
        '{"facts": ["fact1"], '
        '"relations": [{"source": "main.py", "target": "retry", "relation_type": "implements"}]}'
    )
    
    # 3. Call consolidator
    await consolidator.consolidate_episodic_to_semantic_and_graph(
        session_id="session1",
        provider=mock_provider_with_json,
        model_id="test-model",
        embedding_provider=None
    )
    
    # 3. Assertions
    assert mock_provider_with_json.call_count == 1
    assert mock_provider_with_json.last_request is not None
    assert mock_provider_with_json.last_request.model_id == "test-model"
    
    # Verify graph tier node/edge insertion triggered by the mocked JSON
    assert graph.add_node.called
    assert graph.add_edge.called
    
    # Verify archive was called
    assert archive.archive_session.called
    
    # Verify episodic clean up delete was called
    assert episodic.delete_session.called


@pytest.mark.asyncio
async def test_context_compressor_with_mock_provider(mock_provider) -> None:
    """Verify that ContextCompressor triggers LLM-guided context compression correctly."""
    mock_analytics = MagicMock()
    compressor = ContextCompressor(analytics=mock_analytics)
    
    # Text that is large enough to exceed our target budget
    large_text = "\n".join([f"Line number {i} of verbose code comments that must be compressed." for i in range(50)])
    
    # Target budget is very low to force compression
    result = await compressor.compress(
        content=large_text,
        provider=mock_provider,
        model_id="test-model",
        target_token_budget=20
    )
    
    assert mock_provider.call_count == 1
    assert mock_provider.last_request is not None
    assert mock_provider.last_request.model_id == "test-model"
    assert "ok" in result  # Mock provider returns '{"result": "ok"}' by default


@pytest.mark.asyncio
async def test_intent_reconstructor_with_mock_provider(mock_provider_with_json) -> None:
    """Verify that IntentReconstructor reconstructs raw queries into IntentHypothesis using provider JSON."""
    json_response = (
        '{"goal_description": "Fix bug in main.py", '
        '"confidence": 0.95, '
        '"primary_category": "debugging", '
        '"target_files": ["main.py"], '
        '"action_plan": ["Locate bug", "Apply fix", "Run tests"]}'
    )
    mock_provider_with_json.response_content = json_response
    
    reconstructor = IntentReconstructor()
    
    hypothesis = await reconstructor.reconstruct(
        raw_query="please fix the bug in main.py",
        provider=mock_provider_with_json,
        model_id="test-model"
    )
    
    assert mock_provider_with_json.call_count == 1
    assert mock_provider_with_json.last_request is not None
    
    assert isinstance(hypothesis, IntentHypothesis)
    assert hypothesis.goal_description == "Fix bug in main.py"
    assert hypothesis.confidence == 0.95
    assert hypothesis.primary_category == "debugging"
    assert hypothesis.target_files == ["main.py"]
    assert hypothesis.action_plan == ["Locate bug", "Apply fix", "Run tests"]
