"""Unit tests for velune/context/cache/ — provider-agnostic context caching."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.context.cache.fingerprint import ContextFingerprinter
from velune.context.cache.manager import ContextCacheManager, make_cache_manager, reset_all_managers
from velune.context.cache.metrics import CacheMetrics
from velune.context.cache.providers import (
    ANTHROPIC_CACHE_PAYLOAD_KEY,
    AnthropicPromptCacheProvider,
    NoOpCacheProvider,
)
from velune.context.cache.state import CacheState
from velune.core.types.inference import InferenceRequest, InferenceResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    system: str = "You are a helpful assistant.",
    user: str = "TASK: fix the bug\n\nCONTEXT: repo stuff here",
    extra_messages: list | None = None,
) -> InferenceRequest:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if extra_messages:
        messages.extend(extra_messages)
    return InferenceRequest(
        model_id="claude-sonnet-4-5",
        messages=messages,
    )


# ---------------------------------------------------------------------------
# ContextFingerprinter
# ---------------------------------------------------------------------------


class TestContextFingerprinter:
    def test_deterministic(self):
        fp = ContextFingerprinter()
        content = "System prompt v1 with lots of text."
        assert fp.fingerprint(content) == fp.fingerprint(content)

    def test_different_content_different_hash(self):
        fp = ContextFingerprinter()
        assert fp.fingerprint("hello") != fp.fingerprint("world")

    def test_hex_length(self):
        fp = ContextFingerprinter()
        result = fp.fingerprint("some content")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_fingerprint_segments(self):
        fp = ContextFingerprinter()
        segments = {"system": "sys content", "repo": "repo content"}
        result = fp.fingerprint_segments(segments)
        assert set(result.keys()) == {"system", "repo"}
        assert result["system"] == fp.fingerprint("sys content")
        assert result["repo"] == fp.fingerprint("repo content")

    def test_empty_string(self):
        fp = ContextFingerprinter()
        result = fp.fingerprint("")
        assert len(result) == 16

    def test_unicode_content(self):
        fp = ContextFingerprinter()
        result = fp.fingerprint("你好世界 🌏")
        assert len(result) == 16


# ---------------------------------------------------------------------------
# CacheState
# ---------------------------------------------------------------------------


class TestCacheState:
    def test_new_segment_is_not_valid(self):
        state = CacheState()
        assert not state.is_valid("system", "abc123")

    def test_stored_fingerprint_is_valid(self):
        state = CacheState()
        state.update("system", "abc123", action="write")
        assert state.is_valid("system", "abc123")

    def test_changed_fingerprint_is_not_valid(self):
        state = CacheState()
        state.update("system", "abc123", action="write")
        assert not state.is_valid("system", "xyz999")

    def test_hit_counter_incremented(self):
        state = CacheState()
        state.update("system", "abc", action="write")
        state.update("system", "abc", action="hit")
        assert state.writes == 1
        assert state.hits == 1

    def test_miss_counter_incremented(self):
        state = CacheState()
        state.update("system", "abc", action="miss")
        assert state.misses == 1

    def test_invalidate_clears_fingerprints(self):
        state = CacheState()
        state.update("system", "abc", action="write")
        state.update("repo", "def", action="write")
        assert len(state.fingerprints) == 2
        state.invalidate("test")
        assert len(state.fingerprints) == 0

    def test_record_tokens(self):
        state = CacheState()
        state.record_tokens(1000, 500)
        assert state.cached_input_tokens == 1000
        assert state.cache_read_tokens == 500
        state.record_tokens(200, 100)
        assert state.cached_input_tokens == 1200
        assert state.cache_read_tokens == 600


# ---------------------------------------------------------------------------
# CacheMetrics
# ---------------------------------------------------------------------------


class TestCacheMetrics:
    def test_hit_rate_zero_when_no_calls(self):
        m = CacheMetrics()
        assert m.hit_rate == 0.0

    def test_hit_rate_calculation(self):
        m = CacheMetrics(hits=3, misses=1, writes=1)
        assert m.hit_rate == pytest.approx(3 / 5)

    def test_estimated_token_savings(self):
        m = CacheMetrics(cache_read_tokens=1000)
        assert m.estimated_token_savings == 1000

    def test_estimated_cost_savings_positive(self):
        m = CacheMetrics(cache_read_tokens=1_000_000)
        # 1M cache-read tokens at Sonnet pricing saves ~90% of $3.00 = $2.70
        assert m.estimated_cost_savings_usd > 0.0

    def test_merge(self):
        m1 = CacheMetrics(hits=2, writes=1, cached_input_tokens=500, cache_read_tokens=200)
        m2 = CacheMetrics(hits=1, misses=1, cached_input_tokens=300, cache_read_tokens=100)
        m1.merge(m2)
        assert m1.hits == 3
        assert m1.misses == 1
        assert m1.writes == 1
        assert m1.cached_input_tokens == 800
        assert m1.cache_read_tokens == 300


# ---------------------------------------------------------------------------
# NoOpCacheProvider
# ---------------------------------------------------------------------------


class TestNoOpCacheProvider:
    def test_does_not_support_caching(self):
        p = NoOpCacheProvider()
        assert not p.supports_caching()

    def test_prepare_returns_request_unchanged(self):
        p = NoOpCacheProvider()
        req = _make_request()
        result = p.prepare_request(req, [-1, 0])
        assert result is req

    def test_extract_cache_stats_returns_empty(self):
        p = NoOpCacheProvider()
        m = p.extract_cache_stats({"raw_usage": {"cache_creation_input_tokens": 999}})
        assert m.cached_input_tokens == 0
        assert m.cache_read_tokens == 0


# ---------------------------------------------------------------------------
# AnthropicPromptCacheProvider
# ---------------------------------------------------------------------------


class TestAnthropicPromptCacheProvider:
    def test_supports_caching(self):
        p = AnthropicPromptCacheProvider()
        assert p.supports_caching()

    def test_system_message_marked_ephemeral(self):
        p = AnthropicPromptCacheProvider()
        req = _make_request(system="You are the Lead Planner.")
        annotated = p.prepare_request(req, [-1])
        payload = annotated.metadata[ANTHROPIC_CACHE_PAYLOAD_KEY]

        assert isinstance(payload["system"], list)
        assert payload["system"][0]["type"] == "text"
        assert payload["system"][0]["text"] == "You are the Lead Planner."
        assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_user_message_marked_ephemeral(self):
        p = AnthropicPromptCacheProvider()
        req = _make_request(user="CONTEXT: big repo snapshot\nTASK: do the thing")
        annotated = p.prepare_request(req, [-1, 0])
        payload = annotated.metadata[ANTHROPIC_CACHE_PAYLOAD_KEY]

        user_msg = payload["messages"][0]
        assert user_msg["role"] == "user"
        assert isinstance(user_msg["content"], list)
        assert user_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_non_cacheable_message_stays_plain(self):
        p = AnthropicPromptCacheProvider()
        req = _make_request(
            user="stable context",
            extra_messages=[{"role": "user", "content": "dynamic current prompt"}],
        )
        # Only mark index 0 (first user msg) as cacheable, not index 1
        annotated = p.prepare_request(req, [-1, 0])
        payload = annotated.metadata[ANTHROPIC_CACHE_PAYLOAD_KEY]

        second_msg = payload["messages"][1]
        assert second_msg["role"] == "user"
        assert isinstance(second_msg["content"], str)  # not a list — no cache_control

    def test_no_cacheable_indices_returns_request_without_payload(self):
        p = AnthropicPromptCacheProvider()
        req = _make_request()
        result = p.prepare_request(req, [])
        # Empty cacheable list means nothing to cache — original request returned
        assert result is req

    def test_extract_cache_stats_creation(self):
        p = AnthropicPromptCacheProvider()
        m = p.extract_cache_stats({"raw_usage": {"cache_creation_input_tokens": 2500}})
        assert m.writes == 1
        assert m.cached_input_tokens == 2500
        assert m.cache_read_tokens == 0

    def test_extract_cache_stats_reads(self):
        p = AnthropicPromptCacheProvider()
        m = p.extract_cache_stats({"raw_usage": {"cache_read_input_tokens": 3000}})
        assert m.hits == 1
        assert m.cache_read_tokens == 3000
        assert m.cached_input_tokens == 0

    def test_extract_cache_stats_empty_metadata(self):
        p = AnthropicPromptCacheProvider()
        m = p.extract_cache_stats({})
        assert m.writes == 0
        assert m.hits == 0

    def test_original_request_not_mutated(self):
        p = AnthropicPromptCacheProvider()
        req = _make_request()
        original_meta = dict(req.metadata)
        p.prepare_request(req, [-1, 0])
        assert req.metadata == original_meta


# ---------------------------------------------------------------------------
# ContextCacheManager
# ---------------------------------------------------------------------------


class TestContextCacheManager:
    def test_prepare_with_noop_returns_same_request(self):
        manager = ContextCacheManager(NoOpCacheProvider())
        req = _make_request()
        result = manager.prepare(req)
        assert result is req

    def test_prepare_anthropic_injects_cache_payload(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        req = _make_request()
        result = manager.prepare(req)
        assert ANTHROPIC_CACHE_PAYLOAD_KEY in result.metadata

    def test_first_call_is_write(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        req = _make_request()
        manager.prepare(req)
        metrics = manager.get_metrics()
        assert metrics.writes >= 1
        assert metrics.hits == 0

    def test_second_call_same_content_counts_hits(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        req = _make_request()
        manager.prepare(req)
        manager.prepare(req)  # identical content
        metrics = manager.get_metrics()
        assert metrics.hits >= 1

    def test_changed_system_prompt_increments_write(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        manager.prepare(_make_request(system="Prompt v1"))
        manager.prepare(_make_request(system="Prompt v2 — different"))
        metrics = manager.get_metrics()
        # Both system calls should be writes (fingerprint changed)
        assert metrics.writes >= 2

    def test_record_updates_metrics(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        manager.record({"raw_usage": {"cache_creation_input_tokens": 1500, "cache_read_input_tokens": 0}})
        metrics = manager.get_metrics()
        assert metrics.cached_input_tokens == 1500

    def test_get_metrics_returns_copy(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        m1 = manager.get_metrics()
        m2 = manager.get_metrics()
        assert m1 is not m2

    def test_reset_clears_state_and_metrics(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        manager.prepare(_make_request())
        manager.record({"raw_usage": {"cache_creation_input_tokens": 500}})
        manager.reset()
        metrics = manager.get_metrics()
        assert metrics.writes == 0
        assert metrics.cached_input_tokens == 0

    def test_invalidate_clears_fingerprints_only(self):
        manager = ContextCacheManager(AnthropicPromptCacheProvider())
        manager.prepare(_make_request())
        manager.record({"raw_usage": {"cache_creation_input_tokens": 500}})
        manager.invalidate("branch changed")
        metrics = manager.get_metrics()
        # Metrics preserved, but fingerprints gone → next call is a write
        assert metrics.cached_input_tokens == 500
        manager.prepare(_make_request())
        metrics2 = manager.get_metrics()
        assert metrics2.writes >= 1


# ---------------------------------------------------------------------------
# make_cache_manager factory
# ---------------------------------------------------------------------------


class TestMakeCacheManagerFactory:
    def setup_method(self):
        reset_all_managers()

    def teardown_method(self):
        reset_all_managers()

    def test_anthropic_gets_anthropic_provider(self):
        m = make_cache_manager("anthropic")
        assert isinstance(m._provider, AnthropicPromptCacheProvider)

    def test_unknown_provider_gets_noop(self):
        m = make_cache_manager("ollama")
        assert isinstance(m._provider, NoOpCacheProvider)

    def test_openai_gets_noop(self):
        m = make_cache_manager("openai")
        assert isinstance(m._provider, NoOpCacheProvider)

    def test_same_provider_returns_singleton(self):
        m1 = make_cache_manager("anthropic")
        m2 = make_cache_manager("anthropic")
        assert m1 is m2


# ---------------------------------------------------------------------------
# SessionUsageTracker — schema v3 cache columns
# ---------------------------------------------------------------------------


class TestUsageTrackerCacheSchema:
    def test_cache_columns_exist(self, tmp_path: Path):
        from velune.telemetry.usage_tracker import SessionUsageTracker

        tracker = SessionUsageTracker(db_path=tmp_path / "usage.db")
        with sqlite3.connect(tracker.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)")}
        assert "cache_creation_tokens" in cols
        assert "cache_read_tokens" in cols

    def test_record_completion_stores_cache_tokens(self, tmp_path: Path):
        from velune.telemetry.usage_tracker import SessionUsageTracker

        tracker = SessionUsageTracker(db_path=tmp_path / "usage.db")
        tracker.record_completion(
            session_id="sess1",
            model="claude-sonnet-4-5",
            input_tokens=100,
            output_tokens=50,
            provider_id="anthropic",
            cache_creation_tokens=2500,
            cache_read_tokens=0,
        )
        with sqlite3.connect(tracker.db_path) as conn:
            row = conn.execute(
                "SELECT cache_creation_tokens, cache_read_tokens FROM usage_records WHERE session_id='sess1'"
            ).fetchone()
        assert row[0] == 2500
        assert row[1] == 0

    def test_get_cache_stats_returns_aggregated_data(self, tmp_path: Path):
        from velune.telemetry.usage_tracker import SessionUsageTracker

        tracker = SessionUsageTracker(db_path=tmp_path / "usage.db")
        tracker.record_completion(
            "s1", "claude-haiku-4-5", 100, 50, provider_id="anthropic",
            cache_creation_tokens=3000, cache_read_tokens=0,
        )
        tracker.record_completion(
            "s1", "claude-haiku-4-5", 100, 50, provider_id="anthropic",
            cache_creation_tokens=0, cache_read_tokens=3000,
        )
        stats = tracker.get_cache_stats(days=30)
        assert stats["cache_creation_tokens"] == 3000
        assert stats["cache_read_tokens"] == 3000
        assert stats["cache_writes"] == 1
        assert stats["cache_hits"] == 1

    def test_schema_migration_adds_columns_to_existing_db(self, tmp_path: Path):
        """Simulates upgrading a v2 DB (no cache columns) to v3."""
        db_path = tmp_path / "v2.db"
        # Create a v2-style DB manually without cache columns
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE usage_records (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT,
                    estimated_cost REAL
                )
                """
            )
            conn.commit()

        # Instantiate tracker — migration should add the new columns
        from velune.telemetry.usage_tracker import SessionUsageTracker
        tracker = SessionUsageTracker(db_path=db_path)
        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)")}
        assert "cache_creation_tokens" in cols
        assert "cache_read_tokens" in cols


# ---------------------------------------------------------------------------
# InferenceRequest — cache_hints field
# ---------------------------------------------------------------------------


class TestInferenceRequestCacheHints:
    def test_cache_hints_defaults_to_none(self):
        req = _make_request()
        assert req.cache_hints is None

    def test_cache_hints_can_be_set(self):
        req = InferenceRequest(
            model_id="m",
            messages=[],
            cache_hints={-1: "ephemeral", 0: "ephemeral"},
        )
        assert req.cache_hints == {-1: "ephemeral", 0: "ephemeral"}


# ---------------------------------------------------------------------------
# InferenceResponse — cache token fields
# ---------------------------------------------------------------------------


class TestInferenceResponseCacheFields:
    def test_cache_tokens_default_to_zero(self):
        r = InferenceResponse(
            content="hi",
            model_id="m",
            finish_reason="end_turn",
            tokens_used=10,
            latency_ms=50.0,
        )
        assert r.cache_creation_tokens == 0
        assert r.cache_read_tokens == 0

    def test_cache_tokens_can_be_set(self):
        r = InferenceResponse(
            content="hi",
            model_id="m",
            finish_reason="end_turn",
            tokens_used=10,
            latency_ms=50.0,
            cache_creation_tokens=2000,
            cache_read_tokens=1500,
        )
        assert r.cache_creation_tokens == 2000
        assert r.cache_read_tokens == 1500


# ---------------------------------------------------------------------------
# AnthropicProvider integration — mocked HTTP
# ---------------------------------------------------------------------------


class TestAnthropicProviderCacheIntegration:
    @pytest.mark.asyncio
    async def test_infer_parses_cache_tokens_from_response(self):
        from velune.providers.adapters.anthropic import AnthropicProvider

        mock_response_data = {
            "content": [{"text": "Hello world", "type": "text"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 2500,
                "cache_read_input_tokens": 0,
            },
        }

        mock_http_response = MagicMock()
        mock_http_response.raise_for_status = MagicMock()
        mock_http_response.json = MagicMock(return_value=mock_response_data)

        provider = AnthropicProvider(api_key="test-key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_http_response)
        provider.client = mock_client

        req = _make_request()
        response = await provider.infer(req)

        assert response.cache_creation_tokens == 2500
        assert response.cache_read_tokens == 0
        assert response.metadata["raw_usage"]["cache_creation_input_tokens"] == 2500

    @pytest.mark.asyncio
    async def test_infer_uses_cache_payload_when_present(self):
        from velune.providers.adapters.anthropic import AnthropicProvider

        mock_response_data = {
            "content": [{"text": "ok", "type": "text"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 0,
            },
        }
        mock_http_response = MagicMock()
        mock_http_response.raise_for_status = MagicMock()
        mock_http_response.json = MagicMock(return_value=mock_response_data)

        provider = AnthropicProvider(api_key="test-key")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_http_response)
        provider.client = mock_client

        # Pre-inject a cache payload as the manager would do
        cache_payload = {
            "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "stuff"}],
        }
        req = _make_request()
        req = req.model_copy(update={"metadata": {ANTHROPIC_CACHE_PAYLOAD_KEY: cache_payload}})

        response = await provider.infer(req)

        assert response.cache_read_tokens == 3000

        # Verify that the posted payload used the pre-transformed structure
        call_args = mock_client.post.call_args
        posted_json = call_args[1]["json"]  # kwargs
        assert isinstance(posted_json["system"], list)
        assert posted_json["system"][0]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_beta_header_set_on_client_init(self):
        from velune.providers.adapters.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")
        await provider.initialize()
        assert provider.client is not None
        # The client should have been initialised with the beta header
        # httpx.AsyncClient stores headers on its _headers attribute
        header_keys = {k.lower() for k in provider.client.headers.keys()}
        assert "anthropic-beta" in header_keys
        await provider.shutdown()
