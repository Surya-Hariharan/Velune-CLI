"""Tests for the `velune workspace graph` dependency analyzer.

Every assertion checks that the analyzer reports *real* graph structure derived
from ``RepositorySnapshot.edges`` — fan-in/fan-out degree, edge-type filtering,
import-cycle detection, and focused neighbourhood views — and never fabricates
links that the snapshot does not contain.
"""

from __future__ import annotations

from velune.observability.workspace_graph import build_dependency_graph
from velune.repository.schemas import (
    RepositoryEdge,
    RepositoryFile,
    RepositoryLanguage,
    RepositorySnapshot,
)


def _file(path: str) -> RepositoryFile:
    return RepositoryFile(path=path, language=RepositoryLanguage.PYTHON, size_bytes=10, sha256="x")


def _snapshot(files, edges) -> RepositorySnapshot:
    return RepositorySnapshot(
        root_path="/repo",
        files=[_file(f) for f in files],
        symbols=[],
        edges=[RepositoryEdge(source=s, target=t, edge_type=et) for s, t, et in edges],
    )


class TestDegrees:
    def test_fan_in_and_fan_out_counted_from_edges(self):
        snap = _snapshot(
            files=["a.py", "b.py", "c.py"],
            edges=[
                ("a.py", "c.py", "imports"),
                ("b.py", "c.py", "imports"),
                ("a.py", "b.py", "imports"),
            ],
        )
        report = build_dependency_graph(snap)

        assert report.node_count == 3
        assert report.edge_count == 3
        # c.py is imported by a and b -> fan-in 2, imports nothing -> fan-out 0.
        top_in = report.top_fan_in[0]
        assert top_in.path == "c.py"
        assert top_in.fan_in == 2
        assert top_in.fan_out == 0
        # a.py imports b and c -> fan-out 2.
        top_out = report.top_fan_out[0]
        assert top_out.path == "a.py"
        assert top_out.fan_out == 2

    def test_parallel_duplicate_edges_counted_once(self):
        snap = _snapshot(
            files=["a.py", "b.py"],
            edges=[
                ("a.py", "b.py", "imports"),
                ("a.py", "b.py", "imports"),  # duplicate parallel edge
            ],
        )
        report = build_dependency_graph(snap)
        assert report.edge_count == 1
        # Breakdown reconciles with the deduped edge count.
        assert dict(report.edge_type_breakdown)["imports"] == 1

    def test_orphans_are_files_with_no_edges(self):
        snap = _snapshot(
            files=["a.py", "b.py", "lonely.py"],
            edges=[("a.py", "b.py", "imports")],
        )
        report = build_dependency_graph(snap)
        assert report.file_count == 3
        assert report.node_count == 2
        assert report.orphan_count == 1


class TestEdgeTypeFiltering:
    def test_default_filters_to_imports(self):
        snap = _snapshot(
            files=["a.py", "b.py"],
            edges=[
                ("a.py", "b.py", "imports"),
                ("a.py", "sym123", "contains"),
            ],
        )
        report = build_dependency_graph(snap)  # default edge_type="imports"
        assert report.edge_count == 1
        assert report.node_count == 2  # only a.py and b.py via imports
        breakdown = dict(report.edge_type_breakdown)
        assert breakdown == {"imports": 1, "contains": 1}

    def test_can_select_a_different_edge_type(self):
        snap = _snapshot(
            files=["a.py"],
            edges=[("a.py", "sym123", "contains")],
        )
        report = build_dependency_graph(snap, edge_type="contains")
        assert report.edge_type == "contains"
        assert report.edge_count == 1
        assert report.node_count == 2


class TestCycles:
    def test_detects_import_cycle(self):
        snap = _snapshot(
            files=["a.py", "b.py", "c.py"],
            edges=[
                ("a.py", "b.py", "imports"),
                ("b.py", "c.py", "imports"),
                ("c.py", "a.py", "imports"),  # closes the loop
            ],
        )
        report = build_dependency_graph(snap)
        assert len(report.cycles) == 1
        assert set(report.cycles[0]) == {"a.py", "b.py", "c.py"}

    def test_acyclic_graph_has_no_cycles(self):
        snap = _snapshot(
            files=["a.py", "b.py", "c.py"],
            edges=[
                ("a.py", "b.py", "imports"),
                ("b.py", "c.py", "imports"),
            ],
        )
        report = build_dependency_graph(snap)
        assert report.cycles == []

    def test_large_chain_does_not_recurse_overflow(self):
        # Iterative SCC must handle a long chain without hitting recursion limits.
        n = 2000
        files = [f"m{i}.py" for i in range(n)]
        edges = [(f"m{i}.py", f"m{i + 1}.py", "imports") for i in range(n - 1)]
        report = build_dependency_graph(_snapshot(files, edges))
        assert report.cycles == []
        assert report.edge_count == n - 1


class TestFocus:
    def test_focus_resolves_by_suffix_and_lists_neighbours(self):
        snap = _snapshot(
            files=["pkg/a.py", "pkg/b.py", "pkg/c.py"],
            edges=[
                ("pkg/a.py", "pkg/b.py", "imports"),
                ("pkg/c.py", "pkg/a.py", "imports"),
            ],
        )
        report = build_dependency_graph(snap, focus="a.py")
        assert report.focus is not None
        assert report.focus.node == "pkg/a.py"
        assert report.focus.imports == ["pkg/b.py"]
        assert report.focus.imported_by == ["pkg/c.py"]

    def test_focus_tree_respects_depth(self):
        snap = _snapshot(
            files=["a.py", "b.py", "c.py"],
            edges=[
                ("a.py", "b.py", "imports"),
                ("b.py", "c.py", "imports"),
            ],
        )
        shallow = build_dependency_graph(snap, focus="a.py", depth=1)
        assert list(shallow.focus.tree.keys()) == ["b.py"]
        # Depth 1 expands only b.py's entry, not its children.
        assert shallow.focus.tree["b.py"] == {}

        deep = build_dependency_graph(snap, focus="a.py", depth=2)
        assert "c.py" in deep.focus.tree["b.py"]

    def test_focus_tree_marks_cycle_backedge(self):
        snap = _snapshot(
            files=["a.py", "b.py"],
            edges=[
                ("a.py", "b.py", "imports"),
                ("b.py", "a.py", "imports"),
            ],
        )
        report = build_dependency_graph(snap, focus="a.py", depth=4)
        # Expanding from a -> b -> a must stop at the back-edge, not loop forever.
        assert report.focus.tree["b.py"]["a.py"] == {"__cycle__": True}

    def test_ambiguous_focus_reports_candidates(self):
        snap = _snapshot(
            files=["pkg1/util.py", "pkg2/util.py"],
            edges=[("pkg1/util.py", "pkg2/util.py", "imports")],
        )
        report = build_dependency_graph(snap, focus="util.py")
        assert report.focus is None
        assert set(report.focus_candidates) == {"pkg1/util.py", "pkg2/util.py"}

    def test_unknown_focus_yields_no_view_and_no_candidates(self):
        snap = _snapshot(files=["a.py"], edges=[])
        report = build_dependency_graph(snap, focus="does_not_exist.py")
        assert report.focus is None
        assert report.focus_candidates == []


class TestSerialization:
    def test_to_dict_is_json_safe(self):
        import json

        snap = _snapshot(
            files=["a.py", "b.py"],
            edges=[("a.py", "b.py", "imports")],
        )
        report = build_dependency_graph(snap, focus="a.py")
        payload = json.dumps(report.to_dict())  # must not raise
        restored = json.loads(payload)
        assert restored["edge_count"] == 1
        assert restored["focus"]["node"] == "a.py"

    def test_empty_snapshot_is_honest(self):
        report = build_dependency_graph(_snapshot(files=[], edges=[]))
        assert report.node_count == 0
        assert report.edge_count == 0
        assert report.cycles == []
        assert report.top_fan_in == []
