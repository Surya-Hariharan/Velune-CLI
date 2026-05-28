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
