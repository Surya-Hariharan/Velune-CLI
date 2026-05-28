"""Integration tests for Provider API contracts (Batch 13)."""

import pytest
from unittest.mock import MagicMock

from velune.context.compressor import ContextCompressor


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
