# Memory Stabilization Report

**Date**: 2026-06-21  
**Version**: Velune CLI 1.0.0

---

## Memory Architecture Overview

Velune uses a multi-tier memory system:

| Tier | Implementation | Storage |
|------|---------------|---------|
| Working | In-process dict | RAM only |
| Episodic | aiosqlite | `.velune/velune_cognitive_core.db` |
| Semantic | Qdrant | `.velune/qdrant/` |
| Lineage | SQLite pool | `.velune/velune_cognitive_core.db` |
| Graph | networkx in-memory | Rebuilt each session |

---

## Known Issue: `coroutine never awaited`

**Symptom**: `velune memory inspect` raises `RuntimeWarning: coroutine was never awaited`.

**Root Cause**: The `memory inspect` CLI command calls an async method without awaiting it in
certain synchronous code paths.  The async/sync boundary in the memory command handler must
route through `run_async()` from `velune.kernel.entrypoint`.

**Fix**:

In `velune/cli/commands/memory.py`, any call to an async memory API must be wrapped:

```python
from velune.kernel.entrypoint import run_async

# Wrong:
result = memory_tier.inspect()        # coroutine, never awaited

# Correct:
result = run_async(memory_tier.inspect())
```

**Priority**: High  
**Effort**: ~2 hours  
**Risk**: Very low

---

## Qdrant Persistence

Velune configures Qdrant with a disk path (not in-memory) for the semantic memory tier.
The path is derived via:

```python
from velune.core.paths import qdrant_store_path
path = qdrant_store_path(workspace_root)  # → .velune/qdrant/
```

Semantic memory persists across restarts as long as `.velune/qdrant/` is not deleted.

---

## Memory Command Status

| Command | Status | Notes |
|---------|--------|-------|
| `velune memory inspect` | ⚠️ Async boundary issue | Needs `run_async()` wrap |
| `velune memory stats` | ⚠️ Same root cause | Same fix |
| `velune memory persist` | ✅ | Written to SQLite on each episodic record |
| `velune memory gc` | ✅ | Runs compaction via `MemoryCompactor` |

---

## Acceptance Criteria

- `velune memory inspect` returns tier stats without crashing
- `velune memory stats` shows episodic + semantic counts
- Semantic memory entries survive process restart (verified by inspect across sessions)
- No `RuntimeWarning: coroutine was never awaited` in any memory command
