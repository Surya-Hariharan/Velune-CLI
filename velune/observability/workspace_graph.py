"""Derive a truthful module-dependency graph report from a repository snapshot.

``velune workspace graph`` answers a concrete question: *what does the indexed
import graph actually look like?* Velune already computes real file-to-file
import edges during indexing (see
:meth:`velune.repository.cognition.RepositoryCognition._run_pipeline`, which
populates ``RepositorySnapshot.edges``). This module turns those edges into
inspectable architecture metrics — fan-in / fan-out hotspots, import cycles, and
a focused neighbourhood view — without inventing anything.

The analysis here is **pure**: it takes an in-memory
:class:`~velune.repository.schemas.RepositorySnapshot` and returns a
:class:`DependencyGraphReport`. It performs no I/O and starts no runtime, which
keeps it fast and trivially unit-testable with a synthetic snapshot. The CLI
command is responsible for booting the runtime and running the live index; this
module only interprets the result.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.schemas import RepositorySnapshot

# The edge type that represents a module/file dependency. Other edge types in the
# snapshot ("contains" links a file to opaque symbol ids; "calls" links symbols)
# are not file-to-file dependencies, so the default graph view filters to this.
DEFAULT_EDGE_TYPE = "imports"


@dataclass
class GraphNodeStat:
    """Fan-in / fan-out degree for one file node in the dependency graph."""

    path: str
    fan_in: int  # number of files that import this file (depended upon)
    fan_out: int  # number of files this file imports (its dependencies)

    def to_dict(self) -> dict:
        return {"path": self.path, "fan_in": self.fan_in, "fan_out": self.fan_out}


@dataclass
class FocusView:
    """A bounded neighbourhood of the graph centred on one file."""

    node: str
    imports: list[str]  # direct dependencies (what `node` imports)
    imported_by: list[str]  # direct dependents (what imports `node`)
    tree: dict  # nested {path: {child: {...}}} downstream tree, bounded by depth
    depth: int

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "imports": self.imports,
            "imported_by": self.imported_by,
            "tree": self.tree,
            "depth": self.depth,
        }


@dataclass
class DependencyGraphReport:
    """A fully-derived view of the repository's import dependency graph.

    Every field comes from ``snapshot.edges`` and ``snapshot.files``; nothing is
    fabricated. An empty graph yields zeroed counts and empty lists.
    """

    root: str
    edge_type: str
    file_count: int  # total indexed files in the snapshot
    node_count: int  # files participating in at least one edge of `edge_type`
    edge_count: int  # number of `edge_type` edges
    orphan_count: int  # indexed files with no `edge_type` edge in either direction
    edge_type_breakdown: list[tuple[str, int]]  # every edge type present + count
    top_fan_in: list[GraphNodeStat]  # most depended-upon files
    top_fan_out: list[GraphNodeStat]  # files with the most dependencies
    cycles: list[list[str]]  # import cycles (strongly-connected components > 1)
    focus: FocusView | None = None
    focus_candidates: list[str] = field(default_factory=list)  # set when focus is ambiguous

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for ``--json`` output."""
        return {
            "root": self.root,
            "edge_type": self.edge_type,
            "file_count": self.file_count,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "orphan_count": self.orphan_count,
            "edge_type_breakdown": [
                {"edge_type": et, "count": n} for et, n in self.edge_type_breakdown
            ],
            "top_fan_in": [s.to_dict() for s in self.top_fan_in],
            "top_fan_out": [s.to_dict() for s in self.top_fan_out],
            "cycles": self.cycles,
            "focus": self.focus.to_dict() if self.focus else None,
            "focus_candidates": self.focus_candidates,
        }


def _norm(path: str) -> str:
    """Normalise a path to the forward-slash form used by graph node ids."""
    return path.replace("\\", "/")


def _resolve_focus(target: str, nodes: set[str]) -> tuple[str | None, list[str]]:
    """Resolve a (possibly partial) ``target`` path to a single graph node.

    Returns ``(node, [])`` on a unique match, ``(None, candidates)`` when the
    target is ambiguous, and ``(None, [])`` when nothing matches.
    """
    want = _norm(target).strip("/")
    if want in nodes:
        return want, []
    # Suffix match (e.g. "cognition.py" or "repository/cognition.py").
    suffix = [n for n in nodes if n == want or n.endswith("/" + want) or n.endswith(want)]
    if len(suffix) == 1:
        return suffix[0], []
    if len(suffix) > 1:
        # Prefer an exact basename hit if that disambiguates.
        base_exact = [n for n in suffix if n.rsplit("/", 1)[-1] == want.rsplit("/", 1)[-1]]
        if len(base_exact) == 1:
            return base_exact[0], []
        return None, sorted(suffix)[:20]
    return None, []


def _find_cycles(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Return import cycles as strongly-connected components of size > 1.

    Uses an iterative Tarjan's SCC so it is safe on large graphs (no recursion
    limit) and deterministic. Self-loops (a file importing itself, which the
    grapher avoids but we guard for) are reported as single-node cycles.
    """
    index_counter = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    nodes = sorted(adjacency)
    self_loops = [n for n in nodes if n in adjacency.get(n, set())]

    for root in nodes:
        if root in indices:
            continue
        # Iterative DFS: each work item is (node, iterator over its successors).
        work: list[tuple[str, list[str]]] = [(root, sorted(adjacency.get(root, ())))]
        indices[root] = lowlink[root] = index_counter
        index_counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, successors = work[-1]
            progressed = False
            while successors:
                succ = successors.pop(0)
                if succ == node:
                    continue  # self-loop handled separately
                if succ not in indices:
                    indices[succ] = lowlink[succ] = index_counter
                    index_counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, sorted(adjacency.get(succ, ()))))
                    progressed = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], indices[succ])
            if progressed:
                continue
            # All successors processed: close out this node.
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == indices[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))

    for n in self_loops:
        result.append([n])
    # Largest cycles first — they matter most architecturally.
    result.sort(key=lambda c: (-len(c), c))
    return result


def build_dependency_graph(
    snapshot: RepositorySnapshot,
    *,
    edge_type: str = DEFAULT_EDGE_TYPE,
    focus: str | None = None,
    depth: int = 2,
    top_n: int = 15,
) -> DependencyGraphReport:
    """Analyse *snapshot* and return a :class:`DependencyGraphReport`.

    Args:
        snapshot: an indexed repository snapshot (its ``edges`` carry real links).
        edge_type: which edge type to treat as a dependency (default ``imports``).
        focus: optional file path to centre a neighbourhood view on.
        depth: how many hops of downstream imports the focus tree expands.
        top_n: how many fan-in / fan-out hotspots to report.
    """
    all_files = {_norm(f.path) for f in snapshot.files}

    # Count unique directed edges per type. The snapshot is built from a
    # networkx MultiDiGraph, which can carry parallel duplicate edges; deduping
    # on (type, source, target) keeps the breakdown consistent with the degree
    # metrics below, which also treat neighbours as a set.
    seen_edges: set[tuple[str, str, str]] = set()
    breakdown: Counter[str] = Counter()

    out_adj: dict[str, set[str]] = defaultdict(set)
    in_adj: dict[str, set[str]] = defaultdict(set)
    edge_count = 0
    for edge in snapshot.edges:
        src, tgt = _norm(edge.source), _norm(edge.target)
        key = (edge.edge_type, src, tgt)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        breakdown[edge.edge_type] += 1
        if edge.edge_type != edge_type:
            continue
        edge_count += 1
        out_adj[src].add(tgt)
        in_adj[tgt].add(src)

    nodes = set(out_adj) | set(in_adj)

    fan_stats = [
        GraphNodeStat(path=n, fan_in=len(in_adj.get(n, ())), fan_out=len(out_adj.get(n, ())))
        for n in nodes
    ]
    top_fan_in = sorted(fan_stats, key=lambda s: (-s.fan_in, s.path))[:top_n]
    top_fan_out = sorted(fan_stats, key=lambda s: (-s.fan_out, s.path))[:top_n]

    # Orphans: indexed files that take part in no dependency edge at all.
    orphan_count = len(all_files - nodes) if all_files else 0

    cycles = _find_cycles(out_adj)

    focus_view: FocusView | None = None
    focus_candidates: list[str] = []
    if focus:
        node, candidates = _resolve_focus(focus, nodes | all_files)
        if node is not None:
            focus_view = _build_focus(node, out_adj, in_adj, depth=depth)
        else:
            focus_candidates = candidates

    return DependencyGraphReport(
        root=snapshot.root_path,
        edge_type=edge_type,
        file_count=len(all_files),
        node_count=len(nodes),
        edge_count=edge_count,
        orphan_count=orphan_count,
        edge_type_breakdown=breakdown.most_common(),
        top_fan_in=top_fan_in,
        top_fan_out=top_fan_out,
        cycles=cycles[:10],
        focus=focus_view,
        focus_candidates=focus_candidates,
    )


def _build_focus(
    node: str,
    out_adj: dict[str, set[str]],
    in_adj: dict[str, set[str]],
    *,
    depth: int,
) -> FocusView:
    """Build a bounded downstream import tree plus direct-edge lists for *node*."""

    def expand(current: str, remaining: int, ancestry: frozenset[str]) -> dict:
        if remaining <= 0:
            return {}
        children: dict = {}
        for child in sorted(out_adj.get(current, ())):
            if child in ancestry:
                # Stop at a back-edge so cycles don't recurse forever; the cycle
                # itself is reported separately in `cycles`.
                children[child] = {"__cycle__": True}
                continue
            children[child] = expand(child, remaining - 1, ancestry | {child})
        return children

    tree = expand(node, depth, frozenset({node}))
    return FocusView(
        node=node,
        imports=sorted(out_adj.get(node, ())),
        imported_by=sorted(in_adj.get(node, ())),
        tree=tree,
        depth=depth,
    )
