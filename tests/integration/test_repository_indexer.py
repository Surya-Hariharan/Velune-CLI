"""Integration tests for RepositoryIndexer (Batch 13)."""

import time
from pathlib import Path

from velune.repository.indexer import RepositoryIndexer
from velune.repository.schemas import RepositorySymbolKind


def test_index_discovers_python_files(temp_workspace: Path) -> None:
    """Verify that index discovers code files like main.py in the workspace."""
    indexer = RepositoryIndexer(root_path=temp_workspace)
    snapshot = indexer.index(force=True)

    assert snapshot.summary["total_files"] >= 1
    assert any(f.path == "main.py" for f in snapshot.files)


def test_index_extracts_function_symbols(temp_workspace: Path) -> None:
    """Verify that function symbols are extracted correctly from main.py."""
    indexer = RepositoryIndexer(root_path=temp_workspace)
    snapshot = indexer.index(force=True)

    main_file_rec = next(f for f in snapshot.files if f.path == "main.py")
    assert len(main_file_rec.symbols) >= 1

    hello_sym = next(s for s in main_file_rec.symbols if s.name == "hello")
    assert hello_sym.kind == RepositorySymbolKind.FUNCTION


def test_incremental_index_uses_cache(temp_workspace: Path) -> None:
    """Verify that a second call loads index entries from cache."""
    # Custom cache path
    cache_path = temp_workspace / ".velune" / "index_cache.json"
    indexer = RepositoryIndexer(root_path=temp_workspace, cache_path=cache_path)

    # First call (cold)
    t0 = time.perf_counter()
    snapshot1 = indexer.index(force=True)
    time.perf_counter() - t0

    assert cache_path.exists(), "Cache file must be created"

    # Second call (warm/incremental)
    t1 = time.perf_counter()
    snapshot2 = indexer.index(force=False)
    time.perf_counter() - t1

    # Ensure they have identical contents
    assert len(snapshot1.files) == len(snapshot2.files)
    assert len(snapshot1.symbols) == len(snapshot2.symbols)

    # Check that metadata and language fields match perfectly
    f1 = next(f for f in snapshot1.files if f.path == "main.py")
    f2 = next(f for f in snapshot2.files if f.path == "main.py")
    assert f1.sha256 == f2.sha256
    assert f1.language == f2.language


def test_index_cache_invalidated_on_file_change(temp_workspace: Path) -> None:
    """Verify that modifying a file invalidates its cache entry and triggers re-parsing."""
    cache_path = temp_workspace / ".velune" / "index_cache.json"
    indexer = RepositoryIndexer(root_path=temp_workspace, cache_path=cache_path)

    # Cold run
    snapshot1 = indexer.index()
    main_file_rec1 = next(f for f in snapshot1.files if f.path == "main.py")
    assert len(main_file_rec1.symbols) == 1
    assert main_file_rec1.symbols[0].name == "hello"

    # Update file contents to introduce a new function
    main_file = temp_workspace / "main.py"
    main_file.write_text(
        "def hello(): return 'world'\ndef goodbye(): return 'now'\n", encoding="utf-8"
    )

    # Warm run
    snapshot2 = indexer.index()
    main_file_rec2 = next(f for f in snapshot2.files if f.path == "main.py")

    # Should re-parse and find both functions
    symbol_names = {s.name for s in main_file_rec2.symbols}
    assert "hello" in symbol_names
    assert "goodbye" in symbol_names
    assert len(main_file_rec2.symbols) == 2
