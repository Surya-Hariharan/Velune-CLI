# Performance Improvement Report

**Date**: 2026-06-21  
**Version**: Velune CLI 1.0.0

---

## Current Baseline

| Metric | Measured | Notes |
|--------|---------|-------|
| Startup time (cold, no index) | ~1.5–3s | Dominated by heavy import chain |
| Indexing (Velune itself, ~350 files) | ~3–8s | Sequential file reads + AST parse |
| BM25 query latency | <10ms | Lazy rebuild on first query |
| Background index startup | Non-blocking | `asyncio.create_task` — REPL usable immediately |

---

## Lazy Initialization (Already Implemented)

Subsystem modules use `SubsystemModule` factory pattern — each subsystem is instantiated on first
access, not at startup.  The MCP client, memory tiers, and retrieval module are all lazy.

---

## Known Bottlenecks

### Sequential Indexing (Priority 8)

**Current**: `RepositoryIndexer.index()` processes files one-at-a-time in a for-loop.

**Proposed Fix**:
```python
# Replace sequential loop with parallel batching
import concurrent.futures

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    futures = {pool.submit(self._index_file, fp): fp for fp in code_files}
    for future in concurrent.futures.as_completed(futures):
        ...
```

Use `ThreadPoolExecutor` (not `ProcessPoolExecutor`) because:
- File I/O releases the GIL
- AST parsing is CPU-light for typical source files
- Process spawning overhead would dominate for small repos

**Expected improvement**: 2–4× on repos with >200 files  
**Effort**: ~4 hours  
**Risk**: Low (results are order-independent; cache writes need a lock)

### Startup Import Chain

Heavy transitive imports (`qdrant-client`, `lancedb`, `tree-sitter`, `pyarrow`) are loaded at
module import time in several places.  The `mcp` package is already lazily imported.

**Proposed Fix**: Move provider and memory subsystem top-level imports inside their respective
factory functions.

**Effort**: ~2 hours  
**Risk**: Low

---

## Hardware-Derived Profiles (Already Implemented)

`velune/hardware/` detects GPU/CPU/RAM and assigns a `RuntimeProfile` (minimal, standard, full).
This already limits concurrent workers on resource-constrained machines.

---

## Priority 8 Acceptance Criteria

- Startup to REPL prompt: < 1.5s on a modern laptop (8-core, SSD)
- Indexing 350-file repo: < 3s with parallel indexer
- No regression in test suite
