"""Tests for the Repository Knowledge Graph (Sprint 1 / AI Foundation).

Covers:
  - Schema validation
  - KnowledgeGraph persistence (write → read round-trip)
  - KnowledgeGraphBuilder from a synthetic RepositorySnapshot
  - KnowledgeQuery higher-level queries
  - Subgraph traversal
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.query import KnowledgeQuery
from velune.knowledge.schemas import (
    EdgeType,
    KnowledgeEdge,
    KnowledgeGraphStats,
    KnowledgeNode,
    NodeType,
)
from velune.repository.schemas import (
    RepositoryEdge,
    RepositoryFile,
    RepositoryLanguage,
    RepositorySnapshot,
    RepositorySymbol,
    RepositorySymbolKind,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph(tmp_path: Path) -> KnowledgeGraph:
    """A fresh KnowledgeGraph backed by a temp SQLite file."""
    kg = KnowledgeGraph(tmp_path / "test_kg.db")
    asyncio.run(kg.initialize())
    return kg


@pytest.fixture
def sample_nodes() -> list[KnowledgeNode]:
    return [
        KnowledgeNode(
            id="file:main.py", node_type=NodeType.FILE, label="main.py", file_path="main.py"
        ),
        KnowledgeNode(
            id="file:utils.py", node_type=NodeType.FILE, label="utils.py", file_path="utils.py"
        ),
        KnowledgeNode(
            id="sym:main.py:run",
            node_type=NodeType.FUNCTION,
            label="run",
            file_path="main.py",
            line_start=5,
            line_end=20,
        ),
        KnowledgeNode(
            id="sym:utils.py:Helper",
            node_type=NodeType.CLASS,
            label="Helper",
            file_path="utils.py",
            line_start=1,
            line_end=30,
        ),
        KnowledgeNode(
            id="sym:utils.py:Helper.do_work",
            node_type=NodeType.METHOD,
            label="do_work",
            file_path="utils.py",
            line_start=5,
            line_end=15,
        ),
    ]


@pytest.fixture
def sample_edges() -> list[KnowledgeEdge]:
    return [
        KnowledgeEdge(source="file:main.py", target="file:utils.py", edge_type=EdgeType.IMPORTS),
        KnowledgeEdge(source="file:main.py", target="sym:main.py:run", edge_type=EdgeType.DEFINES),
        KnowledgeEdge(
            source="file:utils.py", target="sym:utils.py:Helper", edge_type=EdgeType.DEFINES
        ),
        KnowledgeEdge(
            source="sym:utils.py:Helper",
            target="sym:utils.py:Helper.do_work",
            edge_type=EdgeType.CONTAINS,
        ),
    ]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_node_type_values(self):
        assert NodeType.FILE == "file"
        assert NodeType.CLASS == "class"
        assert NodeType.FUNCTION == "function"
        assert NodeType.METHOD == "method"
        assert NodeType.MODULE == "module"

    def test_edge_type_values(self):
        assert EdgeType.IMPORTS == "imports"
        assert EdgeType.CONTAINS == "contains"
        assert EdgeType.DEFINES == "defines"
        assert EdgeType.INHERITS == "inherits"

    def test_knowledge_node_construction(self):
        node = KnowledgeNode(id="x", node_type=NodeType.FILE, label="x.py")
        assert node.id == "x"
        assert node.node_type == NodeType.FILE
        assert node.metadata == {}

    def test_knowledge_edge_construction(self):
        edge = KnowledgeEdge(source="a", target="b", edge_type=EdgeType.IMPORTS)
        assert edge.weight == 1.0
        assert edge.metadata == {}

    def test_stats_defaults(self):
        stats = KnowledgeGraphStats()
        assert stats.node_count == 0
        assert stats.edge_count == 0


# ---------------------------------------------------------------------------
# KnowledgeGraph persistence tests
# ---------------------------------------------------------------------------


class TestKnowledgeGraphPersistence:
    def test_upsert_and_get_node(self, graph: KnowledgeGraph):
        node = KnowledgeNode(id="n1", node_type=NodeType.FILE, label="a.py", file_path="a.py")
        _run(graph.upsert_node(node))
        fetched = _run(graph.get_node("n1"))
        assert fetched is not None
        assert fetched.id == "n1"
        assert fetched.label == "a.py"
        assert fetched.node_type == NodeType.FILE

    def test_get_nonexistent_node_returns_none(self, graph: KnowledgeGraph):
        result = _run(graph.get_node("does_not_exist"))
        assert result is None

    def test_upsert_node_updates_on_conflict(self, graph: KnowledgeGraph):
        node = KnowledgeNode(id="n1", node_type=NodeType.FILE, label="old.py")
        _run(graph.upsert_node(node))
        updated = KnowledgeNode(id="n1", node_type=NodeType.FILE, label="new.py")
        _run(graph.upsert_node(updated))
        fetched = _run(graph.get_node("n1"))
        assert fetched.label == "new.py"

    def test_upsert_edge_and_neighbors(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))

        nbrs = _run(graph.neighbors("file:main.py", direction="out"))
        targets = {n.id for n, _ in nbrs}
        assert "file:utils.py" in targets
        assert "sym:main.py:run" in targets

    def test_neighbors_direction_in(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))

        nbrs = _run(graph.neighbors("file:utils.py", edge_type=EdgeType.IMPORTS, direction="in"))
        sources = {n.id for n, _ in nbrs}
        assert "file:main.py" in sources

    def test_neighbors_filtered_by_edge_type(
        self, graph: KnowledgeGraph, sample_nodes, sample_edges
    ):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))

        defines_nbrs = _run(
            graph.neighbors("file:main.py", edge_type=EdgeType.DEFINES, direction="out")
        )
        assert len(defines_nbrs) == 1
        assert defines_nbrs[0][0].id == "sym:main.py:run"

    def test_get_nodes_by_type(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        files = _run(graph.get_nodes_by_type(NodeType.FILE))
        assert len(files) == 2
        classes = _run(graph.get_nodes_by_type(NodeType.CLASS))
        assert len(classes) == 1

    def test_get_nodes_by_file(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        nodes = _run(graph.get_nodes_by_file("utils.py"))
        labels = {n.label for n in nodes}
        assert "Helper" in labels
        assert "do_work" in labels

    def test_stats_counts(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))
        stats = _run(graph.stats())
        assert stats.node_count == 5
        assert stats.edge_count == 4
        assert stats.file_count == 2
        assert stats.symbol_count == 3

    def test_clear_removes_all(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))
        _run(graph.clear())
        stats = _run(graph.stats())
        assert stats.node_count == 0
        assert stats.edge_count == 0

    def test_set_and_get_meta(self, graph: KnowledgeGraph):
        _run(graph.set_meta("root_path", "/workspace"))
        value = _run(graph.get_meta("root_path"))
        assert value == "/workspace"

    def test_bulk_upsert_is_idempotent(self, graph: KnowledgeGraph, sample_nodes):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_nodes_bulk(sample_nodes))
        stats = _run(graph.stats())
        assert stats.node_count == len(sample_nodes)

    def test_node_metadata_roundtrip(self, graph: KnowledgeGraph):
        node = KnowledgeNode(
            id="m1",
            node_type=NodeType.FILE,
            label="x.py",
            metadata={"language": "python", "size_bytes": 1024},
        )
        _run(graph.upsert_node(node))
        fetched = _run(graph.get_node("m1"))
        assert fetched.metadata["language"] == "python"
        assert fetched.metadata["size_bytes"] == 1024


# ---------------------------------------------------------------------------
# Subgraph traversal tests
# ---------------------------------------------------------------------------


class TestSubgraph:
    def test_subgraph_from_file_node(self, graph: KnowledgeGraph, sample_nodes, sample_edges):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))

        nodes, edges = _run(graph.subgraph("file:main.py", depth=2))
        node_ids = {n.id for n in nodes}
        # Depth-1: utils.py, sym:main.py:run; Depth-2: sym:utils.py:Helper
        assert "file:main.py" in node_ids
        assert "file:utils.py" in node_ids
        assert "sym:main.py:run" in node_ids

    def test_subgraph_depth_zero_returns_root(self, graph: KnowledgeGraph, sample_nodes):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        nodes, edges = _run(graph.subgraph("file:main.py", depth=0))
        assert any(n.id == "file:main.py" for n in nodes)
        assert len(edges) == 0

    def test_subgraph_filtered_by_edge_type(
        self, graph: KnowledgeGraph, sample_nodes, sample_edges
    ):
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))

        nodes, edges = _run(graph.subgraph("file:main.py", depth=2, edge_types=[EdgeType.IMPORTS]))
        edge_types = {e.edge_type for e in edges}
        assert EdgeType.DEFINES not in edge_types


# ---------------------------------------------------------------------------
# KnowledgeGraphBuilder tests
# ---------------------------------------------------------------------------


class TestKnowledgeGraphBuilder:
    def _make_snapshot(self) -> RepositorySnapshot:
        f1 = RepositoryFile(
            path="app.py",
            language=RepositoryLanguage.PYTHON,
            size_bytes=500,
            sha256="a" * 64,
        )
        f2 = RepositoryFile(
            path="helpers.py",
            language=RepositoryLanguage.PYTHON,
            size_bytes=200,
            sha256="b" * 64,
        )
        s1 = RepositorySymbol(
            name="main",
            kind=RepositorySymbolKind.FUNCTION,
            file_path="app.py",
            line_start=1,
            line_end=10,
        )
        s2 = RepositorySymbol(
            name="Utils",
            kind=RepositorySymbolKind.CLASS,
            file_path="helpers.py",
            line_start=1,
            line_end=20,
        )
        s3 = RepositorySymbol(
            name="compute",
            kind=RepositorySymbolKind.METHOD,
            file_path="helpers.py",
            line_start=5,
            line_end=15,
            parent="Utils",
        )
        edge = RepositoryEdge(source="app.py", target="helpers.py", edge_type="imports")
        return RepositorySnapshot(
            root_path="/workspace",
            files=[f1, f2],
            symbols=[s1, s2, s3],
            edges=[edge],
        )

    def test_build_creates_file_nodes(self, graph: KnowledgeGraph, tmp_path: Path):
        from velune.knowledge.builder import KnowledgeGraphBuilder

        snapshot = self._make_snapshot()
        builder = KnowledgeGraphBuilder(graph)
        _run(builder.build(snapshot, tmp_path))

        files = _run(graph.get_nodes_by_type(NodeType.FILE))
        file_paths = {n.file_path for n in files}
        assert "app.py" in file_paths
        assert "helpers.py" in file_paths

    def test_build_creates_symbol_nodes(self, graph: KnowledgeGraph, tmp_path: Path):
        from velune.knowledge.builder import KnowledgeGraphBuilder

        snapshot = self._make_snapshot()
        builder = KnowledgeGraphBuilder(graph)
        _run(builder.build(snapshot, tmp_path))

        classes = _run(graph.get_nodes_by_type(NodeType.CLASS))
        class_labels = {n.label for n in classes}
        assert "Utils" in class_labels

        functions = _run(graph.get_nodes_by_type(NodeType.FUNCTION))
        func_labels = {n.label for n in functions}
        assert "main" in func_labels

    def test_build_creates_imports_edge(self, graph: KnowledgeGraph, tmp_path: Path):
        from velune.knowledge.builder import KnowledgeGraphBuilder

        snapshot = self._make_snapshot()
        builder = KnowledgeGraphBuilder(graph)
        _run(builder.build(snapshot, tmp_path))

        nbrs = _run(graph.neighbors("file:app.py", edge_type=EdgeType.IMPORTS, direction="out"))
        imported = {n.file_path for n, _ in nbrs}
        assert "helpers.py" in imported

    def test_build_creates_defines_edges(self, graph: KnowledgeGraph, tmp_path: Path):
        from velune.knowledge.builder import KnowledgeGraphBuilder

        snapshot = self._make_snapshot()
        builder = KnowledgeGraphBuilder(graph)
        _run(builder.build(snapshot, tmp_path))

        defines = _run(graph.neighbors("file:app.py", edge_type=EdgeType.DEFINES, direction="out"))
        assert len(defines) >= 1

    def test_rebuild_is_idempotent(self, graph: KnowledgeGraph, tmp_path: Path):
        from velune.knowledge.builder import KnowledgeGraphBuilder

        snapshot = self._make_snapshot()
        builder = KnowledgeGraphBuilder(graph)
        _run(builder.build(snapshot, tmp_path))
        _run(builder.build(snapshot, tmp_path))  # Second build with clear_first=True

        files = _run(graph.get_nodes_by_type(NodeType.FILE))
        assert len(files) == 2  # Not doubled


# ---------------------------------------------------------------------------
# KnowledgeQuery tests
# ---------------------------------------------------------------------------


class TestKnowledgeQuery:
    @pytest.fixture
    def query(self, graph: KnowledgeGraph, sample_nodes, sample_edges) -> KnowledgeQuery:
        _run(graph.upsert_nodes_bulk(sample_nodes))
        _run(graph.upsert_edges_bulk(sample_edges))
        return KnowledgeQuery(graph)

    def test_context_for_file_symbols(self, query: KnowledgeQuery):
        ctx = _run(query.context_for_file("main.py"))
        assert ctx.file_path == "main.py"
        symbol_labels = {s.label for s in ctx.symbols}
        assert "run" in symbol_labels

    def test_context_for_file_imports(self, query: KnowledgeQuery):
        ctx = _run(query.context_for_file("main.py"))
        assert "utils.py" in ctx.imports_from

    def test_context_for_file_imported_by(self, query: KnowledgeQuery):
        ctx = _run(query.context_for_file("utils.py"))
        assert "main.py" in ctx.imported_by

    def test_context_as_text(self, query: KnowledgeQuery):
        ctx = _run(query.context_for_file("main.py"))
        text = ctx.as_text()
        assert "main.py" in text
        assert "run" in text

    def test_importers_of(self, query: KnowledgeQuery):
        importers = _run(query.importers_of("utils.py"))
        assert "main.py" in importers

    def test_dependencies_of(self, query: KnowledgeQuery):
        deps = _run(query.dependencies_of("main.py"))
        assert "utils.py" in deps

    def test_find_classes(self, query: KnowledgeQuery):
        classes = _run(query.find_classes())
        labels = {c.label for c in classes}
        assert "Helper" in labels

    def test_find_functions(self, query: KnowledgeQuery):
        funcs = _run(query.find_functions())
        labels = {f.label for f in funcs}
        assert "run" in labels

    def test_find_by_label_prefix(self, query: KnowledgeQuery):
        results = _run(query.find_by_label("He"))
        labels = {r.label for r in results}
        assert "Helper" in labels

    def test_find_by_label_no_match(self, query: KnowledgeQuery):
        results = _run(query.find_by_label("zzz_nonexistent"))
        assert results == []

    def test_subgraph_for_symbol(self, query: KnowledgeQuery):
        ctx = _run(query.subgraph_for_symbol("sym:utils.py:Helper", depth=1))
        assert ctx.focus_id == "sym:utils.py:Helper"
        node_ids = {n.id for n in ctx.nodes}
        # Helper.do_work should be reachable via CONTAINS
        assert "sym:utils.py:Helper.do_work" in node_ids

    def test_subgraph_as_text(self, query: KnowledgeQuery):
        ctx = _run(query.subgraph_for_symbol("sym:utils.py:Helper", depth=1))
        text = ctx.as_text()
        assert "sym:utils.py:Helper" in text

    def test_summary_text_empty_graph(self, graph: KnowledgeGraph):
        kq = KnowledgeQuery(graph)
        text = _run(kq.summary_text())
        assert "No repository knowledge graph" in text

    def test_summary_text_populated(self, query: KnowledgeQuery):
        text = _run(query.summary_text())
        assert "files" in text
        assert "symbols" in text

    def test_context_for_files_batch(self, query: KnowledgeQuery):
        ctxs = _run(query.context_for_files(["main.py", "utils.py"]))
        assert len(ctxs) == 2
        paths = {c.file_path for c in ctxs}
        assert "main.py" in paths
        assert "utils.py" in paths
