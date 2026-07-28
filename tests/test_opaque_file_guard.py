"""Regression tests for the oversized-file (opaque) parse guard.

``docs/REPOSITORY_INTELLIGENCE_BASELINE.md`` §6.4 found that a single
800KB/60,000-line vendored file sitting in an unrecognized directory
produced 20,013 of 20,015 total symbols and pushed a ~30ms index to 4.3s —
the sharpest scalability finding in the benchmark, since no size guard
existed at the parsing layer (only discovery-time directory-name exclusion,
which doesn't catch `vendor/`-style directories the scanner never learned).

``MAX_STRUCTURAL_PARSE_BYTES`` (schemas.py) and the three call sites that
check it (RepositoryIndexer.index, IncrementalIndexer._index_one,
KnowledgeGraphPatcher._parse_file) are what closes this gap: a file over the
limit is still discovered, hashed, and language-tagged, but never read for
structural (AST/tree-sitter/regex) parsing.
"""

from __future__ import annotations

import asyncio

from velune.intelligence.graph_patcher import KnowledgeGraphPatcher
from velune.knowledge.graph import KnowledgeGraph
from velune.repository.incremental_indexer import IncrementalIndexer, IndexDelta
from velune.repository.indexer import RepositoryIndexer
from velune.repository.schemas import MAX_STRUCTURAL_PARSE_BYTES


def _write_oversized_py(root, rel_path="big.py"):
    # Comfortably over the limit, still fast to write/parse-if-it-were-parsed.
    lines = [f"def fn_{i}():\n    return {i}\n\n" for i in range(22000)]
    content = "".join(lines)
    assert len(content.encode("utf-8")) > MAX_STRUCTURAL_PARSE_BYTES
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_repository_indexer_treats_oversized_file_as_opaque(tmp_path):
    _write_oversized_py(tmp_path)
    (tmp_path / "small.py").write_text("def real():\n    return 1\n", encoding="utf-8")

    snapshot = RepositoryIndexer(tmp_path).index(force=True)

    big = next(f for f in snapshot.files if f.path == "big.py")
    small = next(f for f in snapshot.files if f.path == "small.py")

    assert big.metadata.get("opaque") is True
    assert len(big.symbols) == 0
    assert big.sha256  # still hashed
    assert big.language.value == "python"  # still language-tagged
    assert len(small.symbols) > 0  # untouched real files still parse normally


async def _apply(root, state_path, delta):
    return await IncrementalIndexer(root, state_path).apply_delta(delta)


def test_incremental_indexer_treats_oversized_file_as_opaque(tmp_path):
    _write_oversized_py(tmp_path)
    state_path = tmp_path / ".velune" / "index_state.json"

    state = asyncio.run(_apply(tmp_path, state_path, IndexDelta(to_add=["big.py"])))

    entry = state.file_index["big.py"]
    assert entry.symbol_count == 0
    assert entry.content_hash  # still hashed
    assert entry.language == "python"


async def _patch(root, delta):
    graph = KnowledgeGraph(root / ".velune" / "kg.db")
    await graph.initialize()
    patcher = KnowledgeGraphPatcher(graph, root)
    result = await patcher.patch(delta)
    return graph, result


def test_knowledge_graph_patcher_treats_oversized_file_as_opaque(tmp_path):
    _write_oversized_py(tmp_path)

    async def run():
        graph, result = await _patch(tmp_path, IndexDelta(to_add=["big.py"]))
        node = await graph.get_node("file:big.py")
        return result, node

    result, node = asyncio.run(run())

    assert result.files_patched == 1
    assert node is not None
    assert node.metadata.get("opaque") is True
    # No symbol nodes should have been created for the oversized file.
    assert result.nodes_added == 1  # just the file node
    assert result.edges_added == 0
    # An opaque node asserts less than a fully-parsed one — lower confidence,
    # not the default 1.0 a real structural parse would carry.
    assert node.confidence < 1.0
    assert "opaque_size_guard" in node.provenance
