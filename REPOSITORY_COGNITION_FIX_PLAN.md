# Repository Cognition Fix Plan

**Date**: 2026-06-21  
**Status**: Architecture documented; retrieval integration operational for new sessions

---

## Current State

### What Works

| Component | Status | Notes |
|-----------|--------|-------|
| `RepositoryIndexer` | ✅ | AST parsing, SHA-256 incremental cache, secret detection |
| `RepositorySnapshot` | ✅ | Files + symbols + edges populated via `_run_pipeline` |
| `RepositoryGrapher` | ✅ | import graph edges resolved via `resolve_import_dependencies` |
| `_save_retrieval_index` | ✅ | Writes `.velune/retrieval_index.json` after every full index |
| `retrieval.module` BM25 load | ✅ | Reads `retrieval_index.json` on startup and populates BM25 |
| `HybridRetriever` lexical search | ✅ | BM25 retrieval from populated index |
| `velune context` / `velune trace` | ✅ | Prove indexing/execution from on-disk state |

### Known Gap: Cold-Start Session

When a fresh session starts and `.velune/retrieval_index.json` does not yet exist (first run on a
new workspace), the `BM25Retriever` is empty and lexical queries return zero hits. The index is
written the first time `RepositoryCognitionService.index()` or `_run_pipeline()` is called, which
happens asynchronously in the background after startup.

**Symptom**: `velune pipeline trace "authentication"` on a fresh workspace returns zero hits until
the background index completes.

### Graph Generation

| Metric | Value |
|--------|-------|
| Files indexed (Velune itself) | ~350+ |
| Import symbols detected | ~183 (from parser) |
| Graph edges generated | Proportional to resolved imports |

The `resolve_import_dependencies` method in `RepositoryGrapher` correctly maps:
- Python `from X import Y` → module resolution via dotted name + stem lookup
- TypeScript/JS relative imports (`./`, `../`) → path resolution
- `index.ts` / `__init__.py` → parent directory resolution

---

## Pipeline: How Documents Reach BM25

```
RepositoryIndexer.index()
  → RepositorySnapshot (files + symbols)
  → RepositoryCognitionService._run_pipeline(snapshot)
      → RepositoryGrapher (builds edge graph)
      → GitTracker (git metrics)
      → CodebaseAnalyzer (layers, violations)
      → TechnologyDetector, ArchitectureDetector
      → APIMapper
      → _save_retrieval_index(snapshot)         ← writes .velune/retrieval_index.json
         (id=file_path, content=filename + symbols + language)

On next startup:
retrieval.module._create_hybrid_retriever(env)
  → reads retrieval_index.json
  → BM25Retriever.add_documents_batch(docs)    ← populates lexical index
  → HybridRetriever ready for queries
```

---

## Remaining Work (Priorities 2–3)

### Priority 2: Zero-hit on Fresh Workspace

**Problem**: First session has no `retrieval_index.json` → BM25 empty → all queries return 0 hits.

**Fix Plan**:
1. During background indexing completion (`_background_apply`), additionally call
   `_save_retrieval_index` and signal the retriever module to reload.
2. Or: run a minimal synchronous indexing pass at startup (fast-path SHA check) before accepting
   the first query, deferring the full pipeline to the background.

**Effort**: ~1 day  
**Risk**: Low

### Priority 3: Graph Edges Visibility

**Problem**: `velune workspace graph` shows 0 edges when graph was built but not serialized.

**Fix Plan**:
1. Persist the graph to `.velune/graph.json` after `_run_pipeline`.
2. The `workspace graph` command reads this file for display.

**Effort**: ~0.5 day  
**Risk**: Low

---

## Acceptance Criteria (After Fix)

- `velune pipeline trace "authentication"` → ≥1 hit on a workspace with auth-related files
- `velune pipeline trace "navigation"` → ≥1 hit on a workspace with navigation code
- `velune workspace graph` → connected nodes > 0, edges > 0

---

## No Changes Made to Core Indexing Logic

The existing architecture is correct and does not require rewrites.  The gap is solely in the
**timing of BM25 population** (cold start) and **graph serialization** for the workspace graph
command.  Incremental fixes are preferred over architectural rewrites.
