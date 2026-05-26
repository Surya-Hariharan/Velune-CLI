"""Batch 03 unit tests — provider.infer() API fix across three subsystems."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared MockProvider — implements the ModelProvider protocol correctly
# ---------------------------------------------------------------------------

class MockProvider:
    """Minimal ModelProvider implementation for testing the infer() contract."""

    def __init__(self, response_content: str = None):
        self._response_content = response_content or json.dumps(
            {"facts": ["test fact"], "relations": []}
        )
        self.infer_calls: list = []

    async def infer(self, request):
        from velune.core.types.inference import InferenceResponse
        self.infer_calls.append(request)
        return InferenceResponse(
            content=self._response_content,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=50,
            latency_ms=100.0,
        )


# ---------------------------------------------------------------------------
# Fix 1 — MemoryConsolidator.consolidate_episodic_to_semantic_and_graph
# ---------------------------------------------------------------------------

class TestMemoryConsolidatorCallsInfer:
    def _make_consolidator(self):
        """Build a MemoryConsolidator with all tiers fully mocked."""
        from velune.memory.consolidator import MemoryConsolidator
        from velune.memory.tiers.episodic import EpisodicTurn

        working = MagicMock()
        working.get_turns.return_value = []
        working.get_execution_logs.return_value = []

        # Supplies a real EpisodicTurn so the prompt builds correctly
        episodic = MagicMock()
        episodic.get_turns.return_value = [
            EpisodicTurn(
                session_id="sess-1",
                role="user",
                content="fix the divide-by-zero bug",
                timestamp=1000.0,
            )
        ]
        episodic.get_execution_steps.return_value = []
        episodic.delete_session = MagicMock()

        graph = MagicMock()
        graph.add_node = MagicMock()
        graph.add_edge = MagicMock()

        archive = MagicMock()
        archive.archive_session = MagicMock()

        return MemoryConsolidator(
            working_tier=working,
            episodic_tier=episodic,
            semantic_tier=MagicMock(),
            graph_tier=graph,
            archive_tier=archive,
        )

    @pytest.mark.asyncio
    async def test_memory_consolidator_calls_infer_not_complete(self):
        """consolidate_episodic_to_semantic_and_graph must call provider.infer(), not provider.complete()."""
        consolidator = self._make_consolidator()
        provider = MockProvider(
            response_content=json.dumps(
                {"facts": ["User wants div-zero fix"], "relations": []}
            )
        )

        await consolidator.consolidate_episodic_to_semantic_and_graph(
            session_id="sess-1",
            provider=provider,
            model_id="test-model",
        )

        assert len(provider.infer_calls) == 1, (
            "provider.infer() must be called exactly once during consolidation. "
            f"Got {len(provider.infer_calls)} calls."
        )

    @pytest.mark.asyncio
    async def test_consolidator_infer_request_shape(self):
        """InferenceRequest passed to infer() must have correct shape."""
        from velune.core.types.inference import InferenceRequest

        consolidator = self._make_consolidator()
        provider = MockProvider()

        await consolidator.consolidate_episodic_to_semantic_and_graph(
            session_id="sess-1",
            provider=provider,
            model_id="claude-3",
        )

        assert provider.infer_calls, "provider.infer() was never called"
        req = provider.infer_calls[0]

        assert isinstance(req, InferenceRequest)
        assert req.model_id == "claude-3"
        assert req.temperature == 0.2, f"Expected temperature=0.2, got {req.temperature}"
        assert req.max_tokens == 2000, f"Expected max_tokens=2000, got {req.max_tokens}"
        assert len(req.messages) == 1
        assert req.messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_consolidator_does_not_call_complete(self):
        """provider.complete() must NOT exist on MockProvider — confirms no fallback to old API."""
        consolidator = self._make_consolidator()
        provider = MockProvider()

        assert not hasattr(provider, "complete"), (
            "MockProvider must not have a .complete() method."
        )

        # Must not raise AttributeError — only infer() is called
        await consolidator.consolidate_episodic_to_semantic_and_graph(
            session_id="sess-1",
            provider=provider,
            model_id="test-model",
        )


# ---------------------------------------------------------------------------
# Fix 2 — ContextCompressor.compress
# ---------------------------------------------------------------------------

class TestContextCompressorCallsInfer:
    def _make_compressor(self):
        from velune.context.compressor import ContextCompressor
        with patch("velune.context.compressor.ContextCompressor.__init__", lambda self, a=None: None):
            compressor = ContextCompressor.__new__(ContextCompressor)
        compressor.analytics = MagicMock()
        compressor.analytics.record_compression = MagicMock()
        return compressor

    @pytest.mark.asyncio
    async def test_context_compressor_calls_infer(self):
        """ContextCompressor.compress() must call provider.infer(), not provider.complete()."""
        compressor = self._make_compressor()
        long_content = "This is some regular content. " * 300  # well over budget

        provider = MockProvider(response_content="Compressed summary of the content.")

        result = await compressor.compress(
            content=long_content,
            provider=provider,
            model_id="gpt-4",
            target_token_budget=100,
        )

        assert len(provider.infer_calls) == 1, (
            f"provider.infer() must be called once. Got {len(provider.infer_calls)} calls."
        )
        assert result  # result must be a non-empty string

    @pytest.mark.asyncio
    async def test_compressor_infer_request_shape(self):
        """InferenceRequest must use temperature=0.1 and max_tokens=target_budget*2."""
        from velune.core.types.inference import InferenceRequest

        compressor = self._make_compressor()
        long_content = "word " * 500
        budget = 100

        provider = MockProvider(response_content="compressed")
        await compressor.compress(
            content=long_content,
            provider=provider,
            model_id="test-model",
            target_token_budget=budget,
        )

        assert provider.infer_calls, "provider.infer() was never called"
        req = provider.infer_calls[0]

        assert isinstance(req, InferenceRequest)
        assert req.temperature == 0.1, f"Expected temperature=0.1, got {req.temperature}"
        assert req.max_tokens == budget * 2, (
            f"Expected max_tokens={budget * 2}, got {req.max_tokens}"
        )

    @pytest.mark.asyncio
    async def test_compressor_no_llm_call_when_fits_budget(self):
        """If content already fits within budget, provider.infer() must NOT be called."""
        compressor = self._make_compressor()
        provider = MockProvider()

        await compressor.compress(
            content="short",
            provider=provider,
            model_id="test-model",
            target_token_budget=1000,
        )

        assert len(provider.infer_calls) == 0, (
            "provider.infer() must not be called when content fits within budget. "
            f"Got {len(provider.infer_calls)} calls."
        )


# ---------------------------------------------------------------------------
# Fix 3 — IntentReconstructor.reconstruct
# ---------------------------------------------------------------------------

class TestIntentReconstructorCallsInfer:
    def _make_reconstructor(self):
        from velune.intent.reconstructor import IntentReconstructor
        return IntentReconstructor()

    @pytest.mark.asyncio
    async def test_intent_reconstructor_calls_infer(self):
        """IntentReconstructor.reconstruct() must call provider.infer(), not provider.complete()."""
        reconstructor = self._make_reconstructor()
        provider = MockProvider(
            response_content=json.dumps({
                "goal_description": "Fix the divide-by-zero bug in math_ops.py",
                "confidence": 0.92,
                "primary_category": "debugging",
                "target_files": ["math_ops.py"],
                "action_plan": ["Read file", "Apply patch", "Run tests"],
            })
        )

        result = await reconstructor.reconstruct(
            raw_query="fix the divide by zero bug",
            provider=provider,
            model_id="test-model",
        )

        assert len(provider.infer_calls) == 1, (
            f"provider.infer() must be called once. Got {len(provider.infer_calls)} calls."
        )
        assert result.goal_description, "Reconstructed hypothesis must have a non-empty goal_description"

    @pytest.mark.asyncio
    async def test_reconstructor_goal_description_populated(self):
        """The reconstructed IntentHypothesis must populate goal_description from LLM response."""
        reconstructor = self._make_reconstructor()
        expected_goal = "Refactor the authentication module to support OAuth2"
        provider = MockProvider(
            response_content=json.dumps({
                "goal_description": expected_goal,
                "confidence": 0.85,
                "primary_category": "coding",
                "target_files": ["auth.py"],
                "action_plan": ["Analyze existing code", "Write OAuth2 handler", "Add tests"],
            })
        )

        result = await reconstructor.reconstruct(
            raw_query="add oauth2 support",
            provider=provider,
            model_id="gpt-4",
        )

        assert result.goal_description == expected_goal, (
            f"Expected goal_description='{expected_goal}', got '{result.goal_description}'"
        )

    @pytest.mark.asyncio
    async def test_reconstructor_infer_request_shape(self):
        """InferenceRequest must use temperature=0.3 and max_tokens=1500."""
        from velune.core.types.inference import InferenceRequest

        reconstructor = self._make_reconstructor()
        provider = MockProvider(
            response_content=json.dumps({
                "goal_description": "test goal",
                "confidence": 0.7,
                "primary_category": "general",
                "target_files": [],
                "action_plan": [],
            })
        )

        await reconstructor.reconstruct(
            raw_query="do something",
            provider=provider,
            model_id="claude-opus",
        )

        assert provider.infer_calls, "provider.infer() was never called"
        req = provider.infer_calls[0]

        assert isinstance(req, InferenceRequest)
        assert req.model_id == "claude-opus"
        assert req.temperature == 0.3, f"Expected temperature=0.3, got {req.temperature}"
        assert req.max_tokens == 1500, f"Expected max_tokens=1500, got {req.max_tokens}"
        assert req.messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_reconstructor_falls_back_on_bad_json(self):
        """When LLM returns invalid JSON, reconstruct() must fall back to heuristic candidate."""
        reconstructor = self._make_reconstructor()
        provider = MockProvider(response_content="not valid json at all {{}}")

        # Must not raise — heuristic fallback should kick in
        result = await reconstructor.reconstruct(
            raw_query="fix the bug",
            provider=provider,
            model_id="test-model",
        )

        assert result is not None, "reconstruct() must return a hypothesis even on JSON parse failure"
        assert result.goal_description, "Fallback hypothesis must have a non-empty goal_description"
