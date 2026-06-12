"""Unit tests for Velune Repository Cognition Phase 1 Refactor."""

import tempfile
from pathlib import Path

from velune.kernel.registry import get_container
from velune.repository.cognition import RepositoryCognitionService
from velune.repository.grapher import RepositoryGrapher
from velune.repository.parser import RepositorySnapshotParser
from velune.repository.schemas import (
    RepositorySymbol,
    RepositorySymbolKind,
    build_qualified_name,
    compute_symbol_id,
)
from velune.retrieval.graph import GraphRetriever


def test_qualified_naming_and_stable_ids() -> None:
    """Verify that build_qualified_name and compute_symbol_id produce correct results."""
    # 1. Qualified name for simple symbol
    q1 = build_qualified_name("velune/cognition/orchestrator.py", "CouncilOrchestrator")
    assert q1 == "velune.cognition.orchestrator.CouncilOrchestrator"

    # 2. Qualified name for nested symbol
    q2 = build_qualified_name(
        "velune/cognition/orchestrator.py", "_execute_full", parent="CouncilOrchestrator"
    )
    assert q2 == "velune.cognition.orchestrator.CouncilOrchestrator._execute_full"

    # 3. Qualified name with absolute paths containing drive letters or backslashes
    q3 = build_qualified_name(
        "C:\\Users\\surya\\OneDrive\\Desktop\\Velune-CLI\\velune\\repository\\schemas.py",
        "RepositorySymbol",
    )
    assert q3 == "velune.repository.schemas.RepositorySymbol"

    # 4. SHA256 stable IDs must be deterministic and identical for same inputs
    id1 = compute_symbol_id("velune/cognition/orchestrator.py", q1, "class")
    id2 = compute_symbol_id("velune/cognition/orchestrator.py", q1, "class")
    assert id1 == id2
    assert len(id1) == 64

    # 5. SHA256 stable IDs must differ for different paths or kinds
    id3 = compute_symbol_id("velune/execution/orchestrator.py", q1, "class")
    assert id1 != id3

    # 6. Automatic Pydantic model validator instantiation
    sym = RepositorySymbol(
        name="State",
        kind=RepositorySymbolKind.CLASS,
        file_path="velune/cognition/state.py",
    )
    assert sym.qualified_name == "velune.cognition.state.State"
    assert sym.symbol_id is not None
    assert len(sym.symbol_id) == 64


def test_parser_caching_efficiency() -> None:
    """Verify that RepositorySnapshotParser caches tree-sitter Parser instances per language."""
    parser = RepositorySnapshotParser()

    # Run a parse once
    code = "class TestClass:\n    pass\n"
    parser.parse(Path("test.py"), code)

    # If tree-sitter is available, the parser cache must have 'python' initialized
    from velune.repository.parser import HAS_TREE_SITTER

    if HAS_TREE_SITTER:
        assert "python" in parser._parsers
        # Grab first instance
        first_instance = parser._parsers["python"]

        # Run parse again
        parser.parse(Path("test2.py"), code)
        second_instance = parser._parsers["python"]

        # Must be the exact same cached object reference
        assert first_instance is second_instance


def test_symbol_collision_avoidance() -> None:
    """Verify that two identical class names in different files construct unique symbol IDs and nodes."""
    # Create two symbols with the same name but in different files
    sym1 = RepositorySymbol(
        name="OrchestrationState",
        kind=RepositorySymbolKind.CLASS,
        file_path="velune/cognition/orchestrator.py",
    )

    sym2 = RepositorySymbol(
        name="OrchestrationState",
        kind=RepositorySymbolKind.CLASS,
        file_path="velune/execution/executor.py",
    )

    # Assert they have unique stable IDs and qualified names
    assert sym1.qualified_name == "velune.cognition.orchestrator.OrchestrationState"
    assert sym2.qualified_name == "velune.execution.executor.OrchestrationState"
    assert sym1.symbol_id != sym2.symbol_id

    # Add both to RepositoryGrapher
    with tempfile.TemporaryDirectory() as temp_dir:
        grapher = RepositoryGrapher(Path(temp_dir))

        grapher.add_file("velune/cognition/orchestrator.py", "python", 100)
        grapher.add_file("velune/execution/executor.py", "python", 100)

        grapher.add_symbol(sym1)
        grapher.add_symbol(sym2)

        # The graph must contain separate nodes for both symbol IDs (no collision)
        assert sym1.symbol_id in grapher.graph
        assert sym2.symbol_id in grapher.graph

        # The raw name 'OrchestrationState' must NOT be in the graph as a node
        assert "OrchestrationState" not in grapher.graph

        # Verify contains edges are isolated
        successors1 = list(grapher.graph.successors("velune/cognition/orchestrator.py"))
        successors2 = list(grapher.graph.successors("velune/execution/executor.py"))

        assert sym1.symbol_id in successors1
        assert sym2.symbol_id in successors2
        assert sym1.symbol_id not in successors2
        assert sym2.symbol_id not in successors1


def test_graph_traversal_consistency() -> None:
    """Verify BFS traversals work starting from symbol ID, qualified name, or raw name."""
    sym = RepositorySymbol(
        name="MyService",
        kind=RepositorySymbolKind.CLASS,
        file_path="velune/kernel/service.py",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        grapher = RepositoryGrapher(Path(temp_dir))
        grapher.add_file("velune/kernel/service.py", "python", 120)
        grapher.add_symbol(sym)

        # 1. Traverse starting from the file path
        t1 = grapher.traverse("velune/kernel/service.py")
        assert sym.symbol_id in t1
        assert "velune/kernel/service.py" in t1

        # 2. Traverse starting from the unique symbol_id
        t2 = grapher.traverse(sym.symbol_id)
        assert sym.symbol_id in t2
        assert "velune/kernel/service.py" in t2

        # 3. Traverse starting from the qualified_name (backward-compatible resolver)
        t3 = grapher.traverse("velune.kernel.service.MyService")
        assert sym.symbol_id in t3
        assert "velune/kernel/service.py" in t3

        # 4. Traverse starting from the raw name (backward-compatible resolver)
        t4 = grapher.traverse("MyService")
        assert sym.symbol_id in t4
        assert "velune/kernel/service.py" in t4


def test_retrieval_compatibility() -> None:
    """Verify GraphRetriever handles lookup resolution of symbols correctly using all formats."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Instantiate a repo service in this temp directory
        repo_service = RepositoryCognitionService(temp_path)

        # Write dummy files to trigger scanning
        file1 = temp_path / "velune" / "cognition" / "orchestrator.py"
        file1.parent.mkdir(parents=True, exist_ok=True)
        file1.write_text("class CouncilOrchestrator:\n    pass\n", encoding="utf-8")

        file2 = temp_path / "velune" / "execution" / "executor.py"
        file2.parent.mkdir(parents=True, exist_ok=True)
        file2.write_text("class CouncilOrchestrator:\n    pass\n", encoding="utf-8")

        # Perform index
        snapshot = repo_service.index(force=True)

        # Verify both symbols exist and have unique IDs
        assert len(snapshot.symbols) >= 2
        syms = [s for s in snapshot.symbols if s.name == "CouncilOrchestrator"]
        assert len(syms) == 2
        assert syms[0].symbol_id != syms[1].symbol_id

        # Register repo service in the global container, then retrieve
        container = get_container()
        container.register_instance("runtime.repository_cognition", repo_service)
        try:
            # Create retriever — it will call get_container().get("runtime.repository_cognition")
            retriever = GraphRetriever()

            # Traverse by raw name (should traverse starting at both nodes since they both match raw name)
            hits = retriever.retrieve("CouncilOrchestrator", depth=1)

            # Traversal successfully finds neighbor files and correct symbols
            assert len(hits) >= 2
            assert any("velune/cognition/orchestrator.py" in h.document.content for h in hits)
            assert any("velune/execution/executor.py" in h.document.content for h in hits)
        finally:
            # Clean up global container state so other tests are not affected
            container._singletons.pop("runtime.repository_cognition", None)
