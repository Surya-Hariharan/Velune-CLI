"""Regression tests for VeluneREPL._retrieve_semantic_context.

The plain REPL chat path used to call ``retrieval.search(text, limit=3)`` —
but ``HybridRetriever`` has no such method/signature (it's
``search(self, query: RetrievalQuery)``, synchronous, and raises
``RuntimeError`` if called from a running event loop). Every call therefore
raised inside the ``try`` and was swallowed by a bare ``except Exception:
return None``, so plain chat turns silently never got retrieved context.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from velune.cli.repl import VeluneREPL
from velune.retrieval.schemas import (
    RetrievalDocument,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalSource,
)


def _make_repl_with_retrieval(retrieval) -> VeluneREPL:
    repl = VeluneREPL.__new__(VeluneREPL)  # bypass heavyweight __init__
    repl.container = MagicMock()
    repl.container.get.return_value = retrieval
    return repl


async def test_retrieve_semantic_context_calls_retrieve_with_a_real_query():
    retrieval = MagicMock()
    hit = RetrievalHit(
        document=RetrievalDocument(id="a.py", content="def a(): pass"),
        score=0.9,
        source=RetrievalSource.VECTOR,
        rank=1,
    )
    retrieval.retrieve = AsyncMock(
        return_value=RetrievalResult(query=RetrievalQuery(text="x"), hits=[hit])
    )
    repl = _make_repl_with_retrieval(retrieval)

    result = await repl._retrieve_semantic_context("how does auth work")

    retrieval.retrieve.assert_awaited_once()
    (call_query,), _ = retrieval.retrieve.call_args
    assert isinstance(call_query, RetrievalQuery)
    assert call_query.text == "how does auth work"
    assert result is not None
    assert "def a(): pass" in result


async def test_retrieve_semantic_context_returns_none_on_empty_hits():
    retrieval = MagicMock()
    retrieval.retrieve = AsyncMock(
        return_value=RetrievalResult(query=RetrievalQuery(text="x"), hits=[])
    )
    repl = _make_repl_with_retrieval(retrieval)

    assert await repl._retrieve_semantic_context("nothing relevant") is None


async def test_retrieve_semantic_context_degrades_silently_when_retrieval_missing():
    repl = _make_repl_with_retrieval(None)
    assert await repl._retrieve_semantic_context("anything") is None


async def test_retrieve_semantic_context_swallows_retriever_errors():
    retrieval = MagicMock()
    retrieval.retrieve = AsyncMock(side_effect=RuntimeError("boom"))
    repl = _make_repl_with_retrieval(retrieval)

    assert await repl._retrieve_semantic_context("anything") is None
