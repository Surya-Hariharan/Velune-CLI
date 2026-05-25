"""Repository cognition pipeline merging AST indices, Git history, and dependency graphs."""

from pathlib import Path

from velune.repository.analyzer import CodebaseAnalyzer
from velune.repository.grapher import RepositoryGrapher
from velune.repository.indexer import RepositoryIndexer
from velune.repository.schemas import (
    RepositoryEdge,
    RepositorySnapshot,
)
from velune.repository.tracker import GitTracker


class RepositoryCognitionService:
    """The unified cognitive entrypoint mapping a workspace's AST structure, Git details, and dependencies."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self.indexer = RepositoryIndexer(self.root_path)
        self.grapher = RepositoryGrapher(self.root_path)
        self.tracker = GitTracker(self.root_path)
        self.analyzer = CodebaseAnalyzer(self.root_path)

    def index(self, force: bool = False) -> RepositorySnapshot:
        """Indexes the repository recursively, computing AST, Git metrics, and dependencies."""
        # 1. Incremental Symbol & File indexing
        snapshot = self.indexer.index(force=force)

        # 2. Add files and symbols into the Dependency Grapher
        file_paths = [f.path for f in snapshot.files]
        for f in snapshot.files:
            self.grapher.add_file(f.path, f.language.value, f.size_bytes)
            for sym in f.symbols:
                self.grapher.add_symbol(sym)

        # 3. Resolve import references and draw dependency edges
        self.grapher.resolve_import_dependencies(file_paths, snapshot.symbols)

        # Extract edges from grapher back to snapshot
        edges: list[RepositoryEdge] = []
        for src, tgt, key, data in self.grapher.graph.edges(keys=True, data=True):
            edges.append(
                RepositoryEdge(
                    source=src,
                    target=tgt,
                    edge_type=data.get("edge_type", "depends"),
                    weight=data.get("weight", 1.0)
                )
            )
        snapshot.edges = edges

        # 4. Integrate Git authorship and volatility metrics
        branch = self.tracker.get_active_branch()
        changes = self.tracker.get_uncommitted_changes()
        recent_commits = self.tracker.get_recent_commits(limit=5)

        # Calculate file volatility (commits in last 90 days)
        file_volatility: dict[str, int] = {}
        for f in snapshot.files:
            file_volatility[f.path] = self.tracker.get_file_volatility(f.path)

        # 5. Architectural layer and pattern analysis
        layers = self.analyzer.classify_architecture_layers(file_paths)

        # Compile edges as tuple list for analyzer
        analyzer_edges = [(e.source, e.target) for e in edges]
        violations = self.analyzer.detect_dependency_violations(layers, analyzer_edges)

        # Load file contents for framework scanner
        code_files: dict[str, str] = {}
        for f in snapshot.files:
            # Only read small files under 100KB for safety
            if f.size_bytes < 100000:
                try:
                    full_path = self.root_path / f.path
                    code_files[f.path] = full_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    pass
        frameworks = self.analyzer.detect_framework_footprint(code_files)

        # 6. Aggregate rich summary
        snapshot.summary.update({
            "git": {
                "active_branch": branch,
                "uncommitted_changes_count": len(changes),
                "uncommitted_changes": changes[:10],  # cap list
                "recent_commits": recent_commits
            },
            "architecture": {
                "layers": {k: len(v) for k, v in layers.items()},
                "violations_count": len(violations),
                "violations": violations[:5],  # cap list
                "frameworks_detected": frameworks
            },
            "metrics": {
                "high_volatility_files": sorted(
                    file_volatility.items(), key=lambda x: x[1], reverse=True
                )[:5]
            }
        })

        return snapshot

    def traverse(self, node_id: str, depth: int = 2) -> list[str]:
        """Queries the RepositoryGrapher to discover neighboring code relationships from a starting node."""
        return self.grapher.traverse(node_id, depth)
