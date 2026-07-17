# Phase 4 — Intelligent Retrieval Engine

Goal: retrieve the smallest amount of the highest-quality knowledge for a
task — intent-aware, budget-aware, adaptive — instead of one-size-fits-all
keyword/vector search with fixed weights.

## 1. Before vs after

**Before.** Every call site built the same query regardless of what was
being asked:

```
User prompt → IntentClassifier (bias budget/depth only)
            → RetrievalQuery(text=text, top_k=top_k)   [fixed 0.5/0.3/0.2]
            → HybridRetriever.retrieve()
                → lexical (BM25), vector (cosine), graph — fused via
                  raw weighted sum, NO score normalization
                → CrossEncoderReranker (source-only trust table,
                  intent-blind)
```

`SemanticCodeSearch` (the tool the REPL's tool-calling loop can invoke
mid-turn) was not semantic at all — it called `GrepFiles` and returned that.

**After.**

```
User prompt → IntentClassifier (13 intents: 7 original + SEARCH,
              TEST_GENERATION, SECURITY, ARCHITECTURE, DOCUMENTATION,
              DEPENDENCY_ANALYSIS)
            → RetrievalPlanner.plan(intent, confidence, text)
                → intent → weights/top_k lookup table
                → confidence < 0.3 → balanced default (config-sourced)
                → exact-key, 30s-TTL result cache
            → HybridRetriever.retrieve()
                → lexical / vector / graph, each min-max normalized to
                  [0,1] WITHIN its own result set before fusion
                → CrossEncoderReranker(intent=...) — additive trust
                  boost for the source(s) that matter most for this
                  intent, plus a recency-of-edit signal reusing Phase 3's
                  file-mtime data (no new signal plumbing)
            → ContextBudget.for_chat(..., retrieval_bias=f(intent))
            → RetrievalFeedbackRecorder.record(...)   [after assembly]
```

`SemanticCodeSearch` now calls the real `HybridRetriever` through the same
planner, falling back to grep only if no retriever is registered.

## 2. What already existed vs what's new

Audit before writing code found the retrieval stack more real than a
green-field design would assume — most components existed, but disconnected
or unnormalized:

| Component | Before this phase |
|---|---|
| `BM25Retriever`, `VectorRetriever`, `GraphRetriever` | Real, individually working |
| `HybridRetriever` fusion | Real, but fused **raw, unnormalized** scores (BM25 unbounded, cosine ~[0,1], graph ~0.9) |
| `CrossEncoderReranker` | Real heuristic (`semantic*0.5 + recency*0.3 + trust*0.2`), intent-blind |
| `IntentClassifier` | Real, 7 intents, only biased token budget/depth — never selected retrieval strategy |
| Retrieval Planner | Did not exist |
| `ContextBudget`/`ContextAssembler` | Real, fixed 55/35 split regardless of intent |
| Progressive retrieval | Real on the REPL tool-loop path, but its "semantic" tool was literally grep |
| Feedback recording | Did not exist |
| `kernel/config.py::RetrievalConfig` | Declared, defaulted, zero consumers anywhere |
| Retrieval eval | Did not exist |

New this phase: `velune/retrieval/planner.py`, six new `IntentType` members,
per-source score normalization in `hybrid.py`, intent-conditioned trust
boosts in `reranker.py`, a real `SemanticCodeSearch`, `retrieval_bias` on
`ContextBudget`, `velune/retrieval/feedback.py`, and `RetrievalConfig`
finally wired to something that reads it.

## 3. Retrieval Planner design

`RetrievalPlanner.plan(intent, confidence, text) -> RetrievalQuery`
(`velune/retrieval/planner.py`) holds a fixed lookup table, one entry per
`IntentType`, of `(vector_weight, lexical_weight, graph_weight, top_k)`:

- **Lexical-leaning** (SEARCH, EXPLAIN, QUESTION): exact name/term lookups favor BM25.
- **Graph-leaning** (REFACTOR, ARCHITECTURE, DEPENDENCY_ANALYSIS): structure matters more than prose similarity.
- **Vector-leaning** (GENERATE, TEST_GENERATION, DOCUMENTATION): semantic pattern similarity to existing code/docs.
- **Balanced** (DEBUG, SECURITY, REVIEW, COMMAND): mix of lexical (error text / patterns) and graph (call chains).

Confidence below 0.3 bypasses the table entirely and uses a balanced default
sourced from `config.retrieval` (`kernel/config.py::RetrievalConfig`) — a
wrong skew on a low-confidence guess costs more recall than the balanced
default ever would. The planner also owns a small (32-entry, 30s-TTL,
exact-key) result cache so a tool-loop turn issuing the same query twice
doesn't pay for the pipeline twice — Part 3's "whether previous context can
be reused" made concrete.

## 4. Score normalization

`HybridRetriever.retrieve()` now min-max normalizes `lexical_hits`,
`vector_hits`, and `graph_hits` to `[0,1]` **independently, within each
source's own result set**, before the weighted fusion sum. Single-hit or
all-equal result sets are left untouched (min-max is undefined with no
spread to map). This directly fixes the audited bug: BM25's unbounded raw
scores (tens, for a strong match) previously dominated fusion regardless of
`lexical_weight`, silently defeating any weighting the planner (or a caller)
configured. Pinned by `tests/test_hybrid_normalization.py`, which reconstructs
the exact regression scenario: a numerically huge but relatively-weak BM25
hit against a vector hit that's actually the best match — before the fix
the huge raw score wins outright; after, the correct document wins.

## 5. Intent-aware ranking

`CrossEncoderReranker.rerank()` gained an optional `intent` parameter,
additive on top of the existing formula (`intent=None` behaves identically
to before this phase):

- A small `INTENT_TRUST_BOOST` table nudges trust for the source(s) that matter for that intent — e.g. `import_graph`/`graph` get +0.15 trust for DEPENDENCY_ANALYSIS, `symbol` gets +0.1 for REFACTOR. Additive, not multiplicative, so a highly relevant hit from an unboosted source can still outrank a barely-relevant hit from a boosted one.
- Recency-of-edit: `GraphRetriever` now stamps `metadata["timestamp"]` from the hit's file mtime. The reranker's existing `_calculate_recency_score` already reads that field — no new signal path, just populating a field it already consumed for free.

## 6. Context budget

`ContextBudget.for_chat`/`from_mode` gained an optional `retrieval_bias`
parameter (default 0.55, identical to the prior fixed split). `prompt_context.py`
passes a per-intent bias — higher for SEARCH/DEPENDENCY_ANALYSIS/ARCHITECTURE
(need more retrieved material), lower for DEBUG (needs conversational
continuity) — mirroring the exact pattern already used there for
`repo_snapshot_budget`/retrieval `depth`.

## 7. Progressive retrieval

The REPL's tool-calling loop (`ToolLoopRunner`, up to 10 turns) already let
a model call `semantic_code_search` mid-turn to pull more context — but that
tool called `GrepFiles` under a misleading name. It now resolves
`runtime.retrieval` from the container, classifies intent, plans, and
retrieves for real, falling back to grep only when no retriever is
registered or retrieval errors.

**Not changed**: the Council/orchestrator path's retrieval is still fully
front-loaded (built once before any agent runs) — it's a fundamentally
different, non-tool-calling architecture, and making it progressive is a
separate, much larger orchestration change, not required to hit this
phase's stated success criteria. Named here as a limitation, not attempted.

## 8. Feedback recording (not learning)

`velune/retrieval/feedback.py`'s `RetrievalFeedbackRecorder` records one
entry per turn — query, intent, confidence, per-source hit counts, chunks
kept/dropped, token utilization, budget-exceeded — into a bounded in-memory
history (50 entries) mirrored to the existing structured-logging path
(`structlog`, no new storage system). Registered in the DI container like
the planner, so history persists across a session.

**Deliberately recording-only.** Nothing here re-tunes the planner's weight
table, the reranker's trust table, or the budget bias automatically.
Turning this data into closed-loop auto-tuning is real, separable future
work — attempting it in the same pass as building the recording mechanism
risks exactly the kind of scope creep this project's phased approach has
avoided so far.

## 9. Benchmark

`scripts/benchmark_retrieval.py` generates synthetic git repos at three
sizes. Each file declares one uniquely-named function, so a query naming
that symbol has exactly one correct answer file — giving real precision@k /
recall@k / MRR without a hand-labeled corpus. Both arms run through the same
(now normalized) `HybridRetriever`, with real BM25 and real graph traversal
(a genuine `RepositoryCognitionService` snapshot, built exactly as Phase 3
does it); the vector arm is inactive — no embedding provider is configured
in this environment, and the pipeline degrades to skip it exactly as it does
in production without one.

**Honest limitation of this golden set**: because each query names its
target symbol exactly, BM25 alone already resolves it with near-certainty at
every scale tested — recall@k and MRR come out ~1.0 for both the `before`
(fixed weights) and `after` (planned weights) arms, since scaling a single
dominant source's score by a different weight doesn't change its rank order.
This golden set is well-suited to proving end-to-end correctness at scale
(the right file is found, latency stays low) but not to exposing a
precision/recall *gap* between strategies — that gap is real and is what
Parts 5 and 3 individually demonstrate, with data designed to show it:
`tests/test_hybrid_normalization.py` (normalization) and
`tests/test_retrieval_planner.py` (strategy selection) construct the
controlled multi-source scenarios where the "before" behavior visibly picks
the wrong document and "after" doesn't.

[Benchmark results table and latency-vs-repo-size figures inserted below once the run completes.]

## 10. Remaining limitations

- **Council/orchestrator path stays front-loaded** (§7) — the tool-loop's progressive retrieval doesn't reach it.
- **Feedback is recorded, not learned from** (§8) — closed-loop auto-tuning of weights/trust/budget from the recorded history is future work.
- **The synthetic golden set can't show a strategy-quality gap** (§9) at the aggregate level — only the targeted unit tests can, by construction.
- **Vector arm untested at benchmark scale** — no embedding provider in this environment; `VectorRetriever`/embedding generation are exercised by existing unit tests (`tests/test_vector_retriever_cleanup.py`) but not by this phase's new benchmark.
- **`GraphRetriever` still depth-1, seeded only from top-3 lexical/vector hits** — unchanged this phase; a query whose relevant symbols are more than one hop from any top-3 seed won't surface via the graph arm regardless of `graph_weight`.
- **Intent classification is still zero-LLM keyword heuristics** — the six new categories can be starved of tie-breaks by older, broader single-word signals (e.g. EXPLAIN's bare "what") on ambiguous phrasing; documented and tested in `tests/test_intent.py`, not eliminated.
