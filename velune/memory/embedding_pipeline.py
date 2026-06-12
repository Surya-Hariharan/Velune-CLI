"""Embedding pipeline: content preparation → Ollama nomic-embed-text → LanceDB upsert.

Architecture
------------
* ``embed_text / embed_batch`` — synchronous embedding (used for search queries).
* ``enqueue`` — non-blocking: drops a turn onto a bounded async queue and returns
  immediately.  The REPL never waits for an embedding call to complete.
* Background worker — drains the queue, calls Ollama, upserts to LanceDB.
  Applies exponential back-off (1 → 2 → 4 … 60 s) when Ollama is unavailable,
  then re-queues the item for a later retry.

Content preparation before embedding
-------------------------------------
1. Code blocks longer than 200 tokens are replaced with a ``[lang block, N tokens]``
   placeholder — raw code degrades embedding quality without adding recall value.
2. A context prefix is prepended: ``"Session YYYY-MM-DD, {role}: "``.
3. The combined text is truncated to 512 tokens (embedding quality falls beyond
   this length for nomic-embed-text).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from velune.context.window import estimate_tokens

logger = logging.getLogger("velune.memory.embedding_pipeline")

_TOKEN_LIMIT = 512
_CODE_TOKEN_LIMIT = 200
_BATCH_CONCURRENCY = 3      # max simultaneous Ollama embed calls
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0
_QUEUE_MAXSIZE = 1_000


# ── Queue item ────────────────────────────────────────────────────────────────


@dataclass
class EmbedQueueItem:
    """One unit of embedding work enqueued for background processing."""

    record_id: str
    turn_id: str
    session_id: str
    role: str
    content: str
    source_type: str
    workspace_root: str
    created_at: float


# ── Content preparation ───────────────────────────────────────────────────────


def _strip_long_code_blocks(text: str) -> str:
    """Replace ```lang ... ``` blocks that exceed ``_CODE_TOKEN_LIMIT`` tokens."""

    def _replace(match: re.Match) -> str:
        block = match.group(0)
        toks = estimate_tokens(block)
        if toks > _CODE_TOKEN_LIMIT:
            lang = (match.group(1) or "code").strip() or "code"
            return f"[{lang} block, {toks} tokens]"
        return block

    return re.sub(r"```(\w*)\n.*?```", _replace, text, flags=re.DOTALL)


def _prepare_content(content: str, role: str, created_at: float) -> str:
    """Return a semantics-friendly, token-capped version of *content*.

    1. Strip long code blocks.
    2. Prepend ``"Session {date}, {role}: "``.
    3. Truncate so the total fits inside ``_TOKEN_LIMIT`` tokens.
    """
    from datetime import datetime

    date_str = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")
    prefix = f"Session {date_str}, {role}: "

    cleaned = _strip_long_code_blocks(content)

    combined = prefix + cleaned
    if estimate_tokens(combined) <= _TOKEN_LIMIT:
        return combined

    # Trim the content (never the prefix) to fit the token budget.
    # Use a char-per-token heuristic (4 chars ≈ 1 token) for truncation.
    budget_chars = max(0, (_TOKEN_LIMIT - estimate_tokens(prefix)) * 4)
    return prefix + cleaned[:budget_chars]


# ── Pipeline ──────────────────────────────────────────────────────────────────


class EmbeddingPipeline:
    """Async embedding pipeline backed by a provider that supports ``embed()``.

    Typical provider: ``OllamaProvider`` with ``nomic-embed-text``.
    If *provider* is ``None`` (Ollama unavailable at startup), the pipeline
    degrades gracefully: queries return ``RuntimeError``, queue items accumulate
    until the worker succeeds or the process exits.

    Lifecycle
    ---------
    ``await pipeline.initialize()`` — start the background worker task.
    ``await pipeline.shutdown()``   — cancel the worker, drain in-flight items.
    """

    def __init__(
        self,
        provider: Any,               # OllamaProvider | None
        store: Any,                  # LanceDBStore
        model_id: str = "nomic-embed-text",
    ) -> None:
        self._provider = provider
        self._store = store
        self._model_id = model_id
        self._queue: asyncio.Queue[EmbedQueueItem] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: asyncio.Task | None = None
        self._running = False
        self._backoff = _BACKOFF_BASE

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(
            self._background_worker(), name="velune.embedding_worker"
        )
        logger.debug("EmbeddingPipeline background worker started (model=%s)", self._model_id)

    async def shutdown(self) -> None:
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.debug("EmbeddingPipeline background worker stopped")

    # ── Direct embedding (synchronous / on-demand) ────────────────────────────

    async def embed_text(self, text: str) -> list[float]:
        """Embed a single string.  Raises ``RuntimeError`` if provider unavailable."""
        if not self._provider:
            raise RuntimeError("No embedding provider configured")
        results = await self._provider.embed([text], self._model_id)
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple strings with bounded concurrency."""
        if not self._provider:
            raise RuntimeError("No embedding provider configured")
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        async def _one(t: str) -> list[float]:
            async with sem:
                res = await self._provider.embed([t], self._model_id)
                return res[0]

        return list(await asyncio.gather(*[_one(t) for t in texts]))

    # ── Background-queue interface ─────────────────────────────────────────────

    def enqueue(self, item: EmbedQueueItem) -> None:
        """Non-blocking enqueue.  Silently drops the item when the queue is full."""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                "Embedding queue full (%d items) — dropping turn %s",
                _QUEUE_MAXSIZE,
                item.turn_id,
            )

    async def embed_turn(self, item: EmbedQueueItem) -> None:
        """Prepare, embed, and upsert a single turn.  Called by the background worker."""
        from velune.memory.storage.lancedb_store import MemoryRecord

        prepared = _prepare_content(item.content, item.role, item.created_at)
        embedding = await self.embed_text(prepared)

        record = MemoryRecord(
            id=item.record_id,
            embedding=embedding,
            content=item.content,
            source_type=item.source_type,
            session_id=item.session_id,
            turn_id=item.turn_id,
            workspace_root=item.workspace_root,
            created_at=item.created_at,
            trust_score=1.0,
        )
        await self._store.upsert([record])
        logger.debug(
            "Indexed %s (session=%s, role=%s)",
            item.record_id,
            item.session_id,
            item.role,
        )

    # ── Background worker ─────────────────────────────────────────────────────

    async def _background_worker(self) -> None:
        """Drain the queue, retrying with exponential back-off on errors."""
        while self._running:
            # Wait up to 1 second for an item; loop to check _running flag.
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self.embed_turn(item)
                self._backoff = _BACKOFF_BASE  # reset on success
                self._queue.task_done()
            except Exception as exc:
                logger.warning(
                    "Embedding failed for turn %s (%s) — retry in %.1fs",
                    item.turn_id, type(exc).__name__, self._backoff,
                )
                # Re-enqueue for retry; drop silently if queue is full.
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    logger.error("Queue full on retry — permanently dropping turn %s", item.turn_id)

                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
