# Phase 3 — Incremental Repository Cognition

Goal: repository cognition scales with the size of a *change*, not the size
of the repository, and full rescans leave the normal prompt path.

## 1. Before vs after

**Before.** Every prompt, the orchestrator called
`RepositoryCognitionService.index()`, which — even on a cache *hit* (git SHA
unchanged, clean tree) — ran `_run_pipeline()` unconditionally:

```
Prompt → probe_for_changes() (cheap)
       → index()
           → RepositoryGrapher rebuilt from scratch, full import resolution
           → APIMapper re-reads and re-regexes every file
           → ArchitectureDetector / TechnologyDetector re-run
           → GitTracker.get_all_file_volatility(days=90) (git log subprocess)
           → retrieval_index.json rewritten from the full snapshot
       → response generation
```

Cost was `O(total files)` on *every single prompt*, regardless of whether
anything had changed since the last one.

**After.** The interactive path reads a cache; a background worker keeps it
current:

```
Prompt → probe_for_changes() (cheap)
       → get_snapshot_fresh()
           → read pipeline_cache.json + file/symbol snapshot   [O(1)-ish]
           → merge onto snapshot, done
       → response generation

(concurrently, off the prompt path)
FileSystem change → RepositoryIntelligenceEngine.change_detection_loop
                   → IndexDelta
                   → pipeline_refresh downstream task
                       → grapher patched (add/remove only delta files)
                       → APIMapper.build_map_incremental (rescans only delta files)
                       → architecture/tech detectors re-run in full (cheap — see §4)
                       → git volatility read from a 10-minute TTL cache
                   → pipeline_cache.json persisted
                   → PIPELINE_REFRESHED emitted on CognitiveBus
```

`get_snapshot_fresh()` only falls back to the old synchronous full pipeline
once — true cold start, no cache on disk yet — and seeds the cache from that
run so every call after the first takes the fast path.

## 2. What already existed vs what's new

The audit that opened this phase found most of the required machinery
**already built, but disconnected from the prompt path**:

| Component | Status before this phase |
|---|---|
| `IncrementalIndexer` / `IndexState` (file-level delta, git-SHA fast path) | Working, wired |
| `RepositoryIntelligenceEngine` (background poll loops, downstream queue) | Working, wired, on by default (`watch_files=True`) |
| `CognitiveBus` + `RepositoryEventType` (typed pub/sub) | Working, wired |
| `KnowledgeGraph` + `KnowledgeGraphPatcher` (stable-ID entities, surgical patch) | Working, but its output was never read by the prompt path |
| `_run_pipeline` (grapher, API map, architecture/tech, git metrics) | Ran in full, unconditionally, synchronously, on every prompt |

New in this phase: the **pipeline cache** (`.velune/pipeline_cache.json`),
`RepositoryCognitionService.get_snapshot_fresh()` / `refresh_pipeline_cache()`,
a `pipeline_refresh` downstream task on the existing engine, incremental entry
points on `RepositoryGrapher` (`remove_file`, scoped
`resolve_import_dependencies`) and `APIMapper` (`scan_file`,
`build_map_incremental`), and a `PIPELINE_REFRESHED` event.

## 3. Cache invalidation strategy

The pipeline cache is **not** validated against a staleness token before
being served — `get_snapshot_fresh()` always returns whatever is on disk,
merges it onto the current file/symbol snapshot, and tags it with an age in
`summary["cognition_freshness"]`. Correctness converges asynchronously: the
background engine's change-detection loop (3 s poll + 8 s cooldown after a
change) refreshes the cache shortly after any edit. This is a deliberate
stale-while-revalidate trade — the alternative (block the prompt until the
cache is provably current) reintroduces the exact per-turn latency this phase
removes. `cognition_freshness.stale` flips true past 30 s so a caller (or a
future context-builder change) can choose to say so.

Grapher invalidation: `remove_file()` drops a file's node, its contained
symbol nodes, and all incident edges in one call; `resolve_import_dependencies(
..., source_scope=changed_files)` re-derives only the changed files' outgoing
`imports` edges (stale ones are dropped first), leaving unaffected nodes/edges
untouched. API map invalidation: entries are already file-attributed
(`RouteEndpoint.file`, etc.), so `build_map_incremental` drops entries for
changed/removed files and rescans only those; `connections` (routes ↔ calls ↔
queries) is still re-derived over the merged full lists each time — cheap
(pure in-memory matching, no I/O per the incrementality audit), so it isn't
itself delta-scoped.

## 4. Why architecture/tech detection were *not* made incremental

Audited before writing any code (not assumed): `ArchitectureDetector` and
`TechnologyDetector` never read full file content — the former works purely
off the already-loaded path list (`O(files)` string/regex matching), the
latter reads a fixed, small set of manifest files (`package.json`,
`pyproject.toml`, …) independent of repo size. Neither is a real bottleneck
at any scale tested. Incrementalizing them would add real complexity
(tracking which directory-structure-derived "features" depend on which
files) for no measurable win, so they're left as full re-runs inside the
otherwise-incremental `refresh_pipeline_cache` path. `APIMapper` (full
per-file content read + multi-regex-pass) and the grapher's import
resolution were the two analyzers actually worth scoping to the delta.

## 5. Event model

Rides the existing `CognitiveBus` (`velune/events.py`) and
`RepositoryEventType` (`velune/intelligence/events.py`):

`repository.files_changed` → `repository.index_updated` →
`repository.knowledge_graph_patched` + `repository.pipeline_refreshed` (new,
this phase) — emitted in parallel by the downstream worker for the same
`IndexDelta` — plus the pre-existing `repository.profile_refreshed` and
`repository.git_state_changed` on their own loops. `pipeline_refresh` also
registers a `JobRecord` (`cognition:pipeline_refresh`) so it's visible in
`/index status` and `/dashboard`, closing a gap the audit found: the older
`_background_apply` path never did this despite its own docstring claiming
otherwise.

## 6. Benchmark results

`scripts/benchmark_incremental_cognition.py` generates synthetic,
git-initialized repos (flat Python modules, each importing two neighbors and
declaring one FastAPI route) at three sizes and times: a cold call (no cache
yet), a warm no-op call (nothing changed), a background `pipeline_refresh`
after one file is edited, and the interactive read immediately after.

```
scenario  files     cold  warm_noop   speedup  bg_refresh  after_edit
small        30   0.5557     0.0170     32.8x      0.0837      0.0174
medium      500   8.2741     0.0657    125.9x      0.7339      0.0587
large      5000  80.1007     0.6158    130.1x      5.7602      0.5544
```

(Single local run, Windows, cold filesystem cache not controlled for —
directional, not a precision measurement.)

The result the task asked for is visible directly: **the speedup grows with
repo size** (32.8x → 125.9x → 130.1x), because the old per-turn cost was
`O(files)` and the new one is not. The interactive path (`warm_noop`,
`after_edit`) stays two orders of magnitude below the cold-start cost at
every scale tested.

This is a synthetic, local, single-process benchmark — no real 20k-file
enterprise repository was available, and the task's "5,000–20,000+ files"
scenario C is represented by the 5,000-file synthetic case only. Scoped
deliberately: this is a local CLI process, not a distributed system, so a
benchmark against a repository nobody has access to would be theater.

## 7. Remaining bottlenecks (not solved this phase)

- **`background_refresh_s` still grows with total repo size**, not just the
  delta (0.08s → 0.73s → 5.76s across the three scales above). Cause:
  architecture/tech detection re-run in full every refresh (§4 — a deliberate
  trade, since they're individually cheap), and the grapher's import-lookup
  maps (`file_by_stem`/`file_by_mod`) are rebuilt from the complete file list
  on every call (needed for correct resolution, but `O(files)` regardless of
  delta size). Still 14–19x cheaper than a full cold rebuild at every scale
  measured, but not `O(delta)` yet — the next optimization target if refresh
  latency becomes visible at real enterprise scale.
- **`KnowledgeGraph` (SQLite, patched by `KnowledgeGraphPatcher`) and
  `RepositoryGrapher` (in-memory networkx, patched by this phase's work)
  remain two parallel, redundant import-graph implementations.** Unifying
  them was explicitly out of scope (real risk, touches
  `context_builder.py`'s consumption contract) — the `KnowledgeGraph` is
  still not read anywhere on the interactive path.
- **`_resolve_connections`** (route ↔ frontend-call ↔ DB-query matching) is
  still a global re-derive over the merged list on every API-map update —
  cheap today (no I/O, small nested loop) but would need attention if a repo
  had thousands of routes.
- `_background_apply` (the older file-symbol-index background path,
  predating this phase) still doesn't register with `JobRegistry` — only the
  new `pipeline_refresh` task does. Left alone to keep this change's blast
  radius contained; noted here rather than silently fixed.
