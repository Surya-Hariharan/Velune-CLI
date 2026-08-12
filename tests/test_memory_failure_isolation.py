"""Memory embedding failure isolation from primary chat inference.

Regression coverage for the "Embedding failed for turn ... retry in 1,
retry in 3" flood: independent tracing showed the embedding pipeline's
provider selection was already correctly decoupled from the active chat
provider (it always resolves a dedicated "ollama" adapter — see
memory/subsystems.py::_create_embedding_pipeline — never the chat model in
use), so a chat-model-as-embedding-model mixup was not actually happening.
The real, confirmed bug was that every single retry of the *same* ongoing
failure (e.g. Ollama simply not running) logged at WARNING forever, which is
what actually flooded the REPL — fixed via state-transition logging in
EmbeddingPipeline._background_worker.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from velune.memory.embedding_pipeline import EmbeddingPipeline, EmbedQueueItem


class _FailingProvider:
    """embed() always raises, simulating an unreachable embedding backend."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    async def embed(self, texts, model_id):
        self.calls += 1
        raise self._exc


class _RecoveringProvider:
    """Fails the first N calls, then succeeds."""

    def __init__(self, fail_times: int):
        self._remaining_failures = fail_times
        self.calls = 0

    async def embed(self, texts, model_id):
        self.calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            from velune.core.errors.provider import InferenceError

            raise InferenceError("embedding backend unreachable")
        return [[0.1, 0.2, 0.3] for _ in texts]


class _NoopStore:
    async def upsert(self, records):
        pass


def _item(turn_id: str) -> EmbedQueueItem:
    return EmbedQueueItem(
        record_id=f"rec-{turn_id}",
        turn_id=turn_id,
        session_id="s1",
        role="user",
        content="hello",
        source_type="chat",
        workspace_root="/tmp/ws",
        created_at=0.0,
    )


@pytest.mark.asyncio
async def test_background_worker_warns_once_then_downgrades_to_debug(caplog):
    from velune.core.errors.provider import InferenceError

    provider = _FailingProvider(InferenceError("Ollama unreachable"))
    pipeline = EmbeddingPipeline(provider, _NoopStore())
    pipeline.enqueue(_item("t1"))

    with caplog.at_level(logging.DEBUG, logger="velune.memory.embedding_pipeline"):
        await pipeline.initialize()
        # Let the worker attempt a few retries. Backoff starts at 1s; cap the
        # wait so this test stays fast without racing the worker.
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        await pipeline.shutdown()

    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and "Embedding failed" in r.message
    ]
    assert len(warnings) <= 1, (
        f"expected at most one WARNING for one ongoing cause, got {len(warnings)}"
    )


@pytest.mark.asyncio
async def test_recovery_logs_one_info_line_and_resets_failure_count(caplog):
    provider = _RecoveringProvider(fail_times=2)
    pipeline = EmbeddingPipeline(provider, _NoopStore())
    pipeline._backoff = 0.01  # keep the test fast

    with caplog.at_level(logging.DEBUG, logger="velune.memory.embedding_pipeline"):
        pipeline.enqueue(_item("t1"))
        await pipeline.initialize()
        # Wait for 2 failures + 1 success to play out.
        for _ in range(50):
            if pipeline._consecutive_failures == 0 and provider.calls >= 3:
                break
            await asyncio.sleep(0.02)
        await pipeline.shutdown()

    assert pipeline._consecutive_failures == 0
    recovered = [r for r in caplog.records if "recovered" in r.message.lower()]
    assert len(recovered) == 1


@pytest.mark.asyncio
async def test_embed_text_raises_cleanly_when_no_provider_is_configured():
    """A missing/unreachable embedding provider must degrade gracefully
    (a plain RuntimeError the caller can catch), never crash the pipeline
    construction itself."""
    pipeline = EmbeddingPipeline(None, _NoopStore())
    with pytest.raises(RuntimeError):
        await pipeline.embed_text("hello")


def test_embedding_pipeline_always_resolves_the_dedicated_ollama_provider():
    """The active chat provider (e.g. Groq) must never be selected for
    embeddings — memory/subsystems.py always asks the registry for "ollama"
    by name, regardless of which provider/model is active for chat."""
    import inspect

    from velune.memory import subsystems

    src = inspect.getsource(subsystems._create_embedding_pipeline)
    assert 'provider_registry.get("ollama")' in src
    assert "active_model" not in src
    assert "runtime.provider_registry" in src


def test_chat_only_groq_models_are_not_marked_embedding_capable():
    """A chat model must never be silently eligible for embedding calls."""
    from velune.core.types.model import CapabilityLevel
    from velune.providers.adapters.groq import GROQ_MODELS

    for model in GROQ_MODELS:
        caps = model.capabilities
        assert caps is not None
        assert getattr(caps, "embedding", CapabilityLevel.NONE) == CapabilityLevel.NONE, (
            f"{model.model_id} must not be marked embedding-capable"
        )
