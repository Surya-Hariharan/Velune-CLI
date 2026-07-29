# Context Intelligence: Architecture Design

*The seventh architectural pillar. Where [Prompt Intelligence](02-prompt-intelligence.md)
answers "how should the prompt be compiled" and
[Cognitive Architecture](03-cognitive-architecture.md) answers "what should
Velune do," Context Intelligence answers a third, different question: **what
information deserves to be in the prompt at all.** It is a design document;
no code changes accompany it.*

---

## Contents

- [0. Current State & Gap Analysis](#0-current-state--gap-analysis)
- [1. Purpose](#1-purpose)
- [2. Responsibilities](#2-responsibilities)
- [3. Design Philosophy & Alternatives Considered](#3-design-philosophy--alternatives-considered)
- [4. Context Sources](#4-context-sources)
- [5. Context Lifecycle](#5-context-lifecycle)
- [6. Context Fusion](#6-context-fusion)
- [7. Context Ranking](#7-context-ranking)
- [8. Context Budgeting](#8-context-budgeting)
- [9. Context Compression](#9-context-compression)
- [10. Context Preservation](#10-context-preservation)
- [11. Context Policies](#11-context-policies)
- [12. Context Pipeline](#12-context-pipeline)
- [13. Public APIs](#13-public-apis)
- [14. Internal APIs](#14-internal-apis)
- [15. Data Models](#15-data-models)
- [16. Class Diagram](#16-class-diagram)
- [17. Sequence Diagram](#17-sequence-diagram)
- [18. Failure Modes](#18-failure-modes)
- [19. Security](#19-security)
- [20. Performance](#20-performance)
- [21. Migration Plan](#21-migration-plan)
- [22. Implementation Phases](#22-implementation-phases)

---

## 0. Current State & Gap Analysis

Verified against source. The headline finding, stronger than in any prior
document in this series: **"Context Intelligence" already exists in
substance — gathering, fusion, ranking, budgeting, and compression are each
implemented, correctly, in six different real modules. Nothing here is a
green-field design. The job is to name the subsystem those modules already
collectively form, close the specific gaps that fall between them, and give
it one entrypoint.**

### 0.1 The six modules that already are Context Intelligence

```text
RetrievalPlanner + HybridRetriever   velune/retrieval/{planner,hybrid}.py
    Fuses lexical (BM25) + vector (embedding) + 1-hop graph arms, each
    min-max normalized, combined by INTENT-TUNED WEIGHTS
    (_INTENT_STRATEGY, planner.py:33-52 — e.g. DEPENDENCY_ANALYSIS:
    vector .15/lexical .2/graph .65; GENERATE: vector .55/lexical .25/
    graph .2), falling back to a balanced default below confidence 0.3.

HeuristicReranker                    velune/retrieval/reranker.py
    Real scoring formula, already tuned:
    0.5·semantic + 0.3·recency + 0.2·trust (_calculate_combined_score,
    lines 139-151). Trust is a fixed per-source table (graph 0.85,
    symbol 0.9, vector/memory 0.7, lexical 0.6) plus a small intent-boost
    table. Recency decays linearly 1.0 (<5 min) -> 0.0 (>30 days).
    Also owns the ONE deduplication that exists today
    (_deduplicate_by_content, lines 199-206, keyed on content[:100].lower()).

ContextBudget                        velune/context/budget.py
    Real, live, intent-aware — NOT just a docstring aspiration.
    _RETRIEVAL_BUDGET_BIAS (prompt_context.py:63-70) maps IntentType to a
    tuned retrieval_bias (SEARCH/DEPENDENCY_ANALYSIS 0.7, ARCHITECTURE
    0.65, REFACTOR/REVIEW 0.6, DEBUG 0.45, default 0.55), looked up and
    passed into ContextBudget.for_chat() on every turn (prompt_context.py:
    88-91). This is exactly the "adaptive allocator" the motivating
    request describes — it already exists, just as an inline dict in a
    handler file rather than a named, owned policy.

ContextCompactor                     velune/memory/compaction.py
    Real semantic compression, not truncation: generates an LLM summary
    (_generate_summary), validates it (quality/refusal/compression-ratio
    checks), and ACTUALLY REPLACES the raw turns in working memory with
    the summary (_replace_turns_with_summary, lines 331-362). Triggered
    automatically from MemoryLifecycleManager.record_turn() on turn-count
    or utilization thresholds (lifecycle.py:289-364).

ContextAssembler                     velune/context/assembler.py
    Canonical 7-section ordering, budget enforcement, section-specific
    trimming — unchanged by this document (§0.9).

build_turn_context                    velune/cli/handlers/prompt_context.py
    The orchestrator gluing all of the above together today: gathers 4
    concurrent sources, appends working memory + current prompt, calls
    ContextAssembler.assemble(). This document's job is to give this
    orchestration a name and an owner, not to replace it.
```

### 0.2 Mapping the request's 8 source categories against what's actually fed today

| Category | Status |
|---|---|
| Repository | **Fed** — hybrid retrieval's lexical/vector/graph arms + repository snapshot |
| Memory | **Fed** — `ThreeBrainCoordinator` fan-out via `MemoryLifecycleManager.retrieve()` |
| Conversation | **Fed** — working-memory replay of `repl._conversation` |
| Knowledge Graph | **Fed, but nested** — one of three sub-arms inside the memory-lifecycle call, not a distinct top-level source a caller can reason about independently |
| Git | **Fed, but only as decorative metadata** — `GitTracker`'s branch/uncommitted-count/recent-commits/volatility get rendered as text *inside* the repository-snapshot chunk (`WorkspaceContextBuilder._build_header`, `context_builder.py:84-93`); a "★" marks high-volatility files in the file index, but nothing ranks or selects on git signal directly |
| Tool Results | **Not fed** — see §0.4 |
| Diagnostics | **Not fed at all** — no lint/test/error source exists in `build_turn_context` |
| Workspace | **Partially fed** — file index / tech-stack detection only; no open-file/session-state notion |

### 0.3 Cross-source deduplication exists, but is scoped to one-third of the problem

`HeuristicReranker._deduplicate_by_content` is real and correctly keyed, but
it runs **only inside `HybridRetriever.retrieve()`**, i.e. only across the
lexical/vector/graph arms that feed *one* of the four concurrent sources. It
never sees memory chunks, repository-snapshot chunks, or lineage chunks. A
file's content can genuinely appear twice in the same assembled context —
once via hybrid retrieval, once via the repository snapshot — with no check
anywhere that would catch it. This is the sharpest concrete gap this
document closes (§6).

### 0.4 Tool results and diagnostics are ephemeral, confirmed

`ToolLoopRunner` keeps invocation results in-memory for the turn only; the
caller (`velune/cli/repl.py:1404-1409`) appends them to `repl._conversation`
as `role="tool"` messages, but **only `role="user"`/`"assistant"` turns are
ever passed to `_record_turn_async`** (grepped, confirmed at all three call
sites) — tool results have no episodic or semantic trace. They survive only
via raw working-memory replay next turn, and vanish silently the moment
`ContextCompactor` replaces old turns with a summary or `compress_conversation`
truncates them. There is no diagnostics (lint/test output) source at all.
This directly motivates the "current error" / "current tool result" gap the
motivating request names.

### 0.5 Two compression mechanisms exist with no documented precedence between them

`ContextCompactor` (real LLM summarization, replaces raw turns) and
`velune/context/extractive.py`'s `compress_conversation` (oldest-first
truncation, gated by `mode_config.context_compression`,
`prompt_context.py:142-148`) can **both** run against the same conversation
today, with no stated ordering or mutual-exclusion rule. This is a design
smell this document resolves explicitly (§9), the same way earlier documents
in this series resolved the "Repository Brain" naming collision
([04-knowledge-layer.md](04-knowledge-layer.md) §0.1) and the two disjoint
classifiers ([03-cognitive-architecture.md](03-cognitive-architecture.md)
§0.2) — not a new problem invented for this document, a real ambiguity
found and named.

### 0.6 A near-complete feature sits orphaned

`SemanticMemory.index_session_summary()` (`velune/memory/tiers/semantic.py:
242-264`) has **zero callers** anywhere in the tree.  `ContextCompactor`
stores its generated summary via `episodic_memory.record_turn` instead —
session summaries are created, validated, and stored, but never separately
embedded for semantic retrieval. This is cheap to wire (§22), not a reason
to design a new summarization path.

### 0.7 A real "how important is this file" signal exists, but only for the Council

`estimate_blast_radius()` (`velune/cognition/orchestrator.py:553-600`,
depth-1/depth-2 dependent counts via `RepositoryGrapher`) has exactly two
callers: Council tier-scaling and an on-demand MCP tool. It is **never**
called from `prompt_context.py` or `ContextAssembler` — chat-turn context has
no execution-importance/dependency-distance-based prioritization today.
`WorkspaceContextBuilder`'s fan-in annotation (`context_builder.py:243,260`)
is decorative text only, not a ranking input.

### 0.8 This is not Prompt Intelligence's `ContextBuilder` — a clean boundary, stated once

[02-prompt-intelligence.md](02-prompt-intelligence.md) §12 already designed
(and this session already implemented, at
`velune/prompt_intelligence/context_builder.py`) a `ContextBuilder` that
wraps `ContextAssembler.assemble()`'s *output* as IR context nodes for the
prompt compiler. That component is downstream of, and unaffected by, this
document: **Context Intelligence owns everything from source-gathering
through producing the final, deduped, ranked, correctly-budgeted
`list[ContextChunk]`; `ContextAssembler` remains the last step (ordering +
render, unchanged); Prompt Intelligence's `ContextBuilder` remains the step
after that (wrap the rendered string as IR, unchanged).** No file, class, or
responsibility is shared between this document and that one once this
boundary is stated — exactly the "every responsibility has exactly one
owner" property a cross-document review should be checking for.

### 0.9 Reconciling with Cognitive Architecture's "Gather Context" step

[03-cognitive-architecture.md](03-cognitive-architecture.md) §5/§13 already
names `CognitiveCore.handle_turn()`'s Gather Context step as calling
"`RepositoryCognitionService` snapshot + `ThreeBrainCoordinator` query +
`PromptCompiler`'s `ContextBuilder`." This document refines that: Gather
Context should call **one** `ContextIntelligence.gather()` entrypoint, which
internally sequences the modules in §0.1 — replacing today's inline
concurrent-gather in `build_turn_context`, not adding a parallel path
alongside it.

### 0.10 A related, out-of-scope observation for the cross-document review

Three overlapping-but-distinct classification granularities now drive three
different decisions: `IntentType` (13 values — drives `_RETRIEVAL_BUDGET_
BIAS` directly, §8), `TaskKind` (9 values — a coarser derived mapping of the
same `IntentType`, drives Prompt Intelligence's template selection), and
`CouncilTier` (4 values — an entirely separate classifier, drives
deliberation depth). This is consistent today (the first two both derive
from one real signal) but is the same fragmentation Cognitive Architecture
§0.2 already named as a gap its proposed `TurnUnderstanding` unification
should eventually close. Flagged here for the cross-document review, not
re-solved in this document.

---

## 1. Purpose

Context Intelligence owns the question "what information deserves to be in
the prompt" — gathering from every real context source, fusing and
deduplicating across all of them (not just within one retrieval arm),
ranking by a formalized multi-factor score, allocating token budget
adaptively per task, and resolving which compression mechanism runs when. It
produces the `list[ContextChunk]` that `ContextAssembler` already consumes
today — it does not replace `ContextAssembler`, `HybridRetriever`,
`HeuristicReranker`, or `ContextCompactor`; it names them, gives them one
entrypoint, and closes the gaps that exist between them.

## 2. Responsibilities

- Own context **source registration and gathering** across all eight
  categories in §4, with Tool Results and Diagnostics as genuinely new
  sources and Git promoted from decorative metadata to a first-class,
  independently-ranked source.
- Own **cross-source fusion and deduplication** — extending
  `HeuristicReranker`'s existing content-key dedup to run across *all*
  gathered chunks, not just the three retrieval arms (§0.3, §6).
- Own **ranking** — formalizing `HeuristicReranker`'s existing
  0.5/0.3/0.2 formula as the house scoring function, with an optional
  fourth dimension (execution importance, reusing `estimate_blast_radius`
  unchanged) for repository-sourced chunks (§7).
- Own **adaptive budget allocation** — formalizing `_RETRIEVAL_BUDGET_BIAS`
  as a registered, extensible policy instead of an inline dict in a handler
  file (§8).
- Own **compression precedence** — stating explicitly which of
  `ContextCompactor` / `compress_conversation` is primary and when the
  other is a fallback (§9).
- Own **preservation policy** — formalizing `ContextAssembler`'s
  `UNTRIMMED_SECTIONS` as the base "never remove" list and extending it to
  cover the most recent tool result/diagnostic (§10).
- Explicitly **not** responsible for: prompt rendering (Prompt
  Intelligence), intent/tier classification (Cognitive Architecture's
  Executive Brain), retrieval algorithm internals (stay in `HybridRetriever`/
  `RetrievalPlanner`, reused unchanged), or durable storage (stays in
  Knowledge Brain's memory tiers / `KnowledgeGraph`).

## 3. Design Philosophy & Alternatives Considered

**Core tenet, carried through every document in this series: reuse, don't
replace — here more than anywhere else, because almost nothing needs to be
newly built.** The work is naming, connecting, and closing five enumerated
gaps (§0.3-§0.7), not designing a ranking or compression algorithm from
scratch.

| Option | Verdict | Why |
|---|---|---|
| **A. Leave everything as scattered, unnamed modules** glued together inline in `prompt_context.py` | Rejected | This is precisely today's state (§0) — every future subsystem (Repository Intelligence, Knowledge Graph) would have to independently learn which of six files to touch; no single place to point at for "what decides what's in the prompt" |
| **B. Rewrite retrieval/ranking/compression from scratch** as one monolithic class | Rejected | `HybridRetriever`, `RetrievalPlanner`, `HeuristicReranker`, `ContextCompactor`, and `ContextAssembler` are each independently real, tested, and correctly scoped (§0.1) — a rewrite is pure risk for zero behavioral change |
| **C. Patch the five enumerated gaps directly into `prompt_context.py`** without naming a subsystem at all | Rejected | Would re-create the exact "no single owner" problem the requested cross-document review is designed to catch |
| **D. Name `ContextIntelligence` as the subsystem these modules already collectively form**, expose one `gather()` entrypoint that sequences them exactly as `build_turn_context` already does, and close the five gaps as targeted extensions to those same modules | **Chosen** | Every extension point in §6-§10 is a small, additive change to a module that already does 90% of the job correctly |

## 4. Context Sources

```text
Repository        HybridRetriever (lexical/vector/graph) + RepositoryCognitionService
                   snapshot — EXISTING, unchanged.

Memory             ThreeBrainCoordinator (working/semantic/episodic) via
                   MemoryLifecycleManager — EXISTING, unchanged.

Conversation         repl._conversation working-memory replay — EXISTING, unchanged.

Knowledge Graph        KnowledgeQuery, currently nested inside the memory-lifecycle
                       call — PROMOTED to a first-class, independently-named source
                       (still the same KnowledgeQuery/KnowledgeGraph underneath, per
                       04-knowledge-layer.md's Graph View, §9 of that document).

Git                      GitTracker's branch/volatility/co-change signals, currently
                         decorative text inside the repo snapshot — PROMOTED to a
                         first-class source with its own ContextChunk and trust
                         score, reusing GitTracker unchanged (§0.2).

Tool Results               NEW — ToolResultSource. Bounded, recent-tool-activity log
                           (a small ring buffer, not a new persistent memory tier)
                           so a later turn sees "you already ran X and got Y"
                           instead of it evaporating (§0.4).

Diagnostics                 NEW — DiagnosticsSource. Surfaces the most recent lint/
                            error output. Reuses the PythonLinter already invoked
                            elsewhere in repl.py (auto-lint on mentioned files) —
                            does not add a new linter.

Workspace                     Covered today only via the repository-snapshot file
                              index. Broader workspace state (open files, terminal
                              output) is named as a future extension (§22), not
                              built now — avoiding speculative scope.
```

## 5. Context Lifecycle

```text
Gather  ->  Fuse+Dedupe  ->  Rank  ->  Budget-allocate  ->  Compress (if needed)
   ->  Preserve (tag never-remove)  ->  hand off list[ContextChunk] to
       ContextAssembler.assemble() (existing, unchanged, §0.8)
```

Each stage below is either a formalization of an existing mechanism or a
small, targeted extension — never a parallel reimplementation.

## 6. Context Fusion

`HeuristicReranker._deduplicate_by_content`'s content-key approach
(`content[:100].lower()`) is reused **unchanged as an algorithm**, but its
*scope* is extended from "the three retrieval arms" to "every chunk gathered
from every source in §4, after all sources return." Concrete rule: when two
chunks from different sources collide on content-key, keep the
higher-trust chunk (per `HeuristicReranker`'s existing trust table, §7) and
merge the losing chunk's `source` into the survivor's metadata — provenance
is preserved, not silently discarded, so a later "why is this in context"
question is always answerable.

## 7. Context Ranking

`HeuristicReranker`'s existing formula —
`0.5·semantic + 0.3·recency + 0.2·trust` — becomes the house scoring
function for every chunk from every source, not just retrieval hits. One new,
optional fourth dimension: `execution_importance`, computed by calling
`estimate_blast_radius()` (reused unchanged from `orchestrator.py`) for
`REPOSITORY_SNAPSHOT`-sourced chunks only, applied as a small additive boost
—not a full re-weighting of the tuned 3-factor formula — and gated behind a
config flag so it can be disabled if it doesn't earn its keep empirically.
This mirrors Input Intelligence's "don't over-correct" caution: an unproven
fourth signal should nudge, not dominate, an already-tuned score.

## 8. Context Budgeting

`_RETRIEVAL_BUDGET_BIAS` (`prompt_context.py:63-70`) becomes
`AdaptiveBudgetAllocator` — the exact same intent→bias table, moved from an
inline dict in a handler file to a registered, extensible policy:

```text
Architecture / Dependency Analysis / Search   ->  retrieval_bias 0.65-0.7
                                                    (Repository ↑, matches request's
                                                     own "Architecture task" example)
Refactor / Review                              ->  retrieval_bias 0.6
Debug                                           ->  retrieval_bias 0.45
                                                     (Conversation ↑ relatively,
                                                      matches request's "Simple chat"
                                                      intuition — debugging leans on
                                                      recent conversational continuity)
default (everything else)                        ->  retrieval_bias 0.55
```

`register_bias(intent_type, bias)` is the extensibility hook — adding a new
intent's budget behavior is one call, not an edit to a hardcoded dict deep
in a handler.

## 9. Context Compression

Explicit precedence, resolving §0.5's ambiguity:

1. **`ContextCompactor` is primary.** Real LLM summarization, triggered on
   turn-count/utilization thresholds exactly as today — unchanged.
2. **`compress_conversation` (truncation) is fallback-only** — runs when
   `ContextCompactor` is unavailable (no summarization model configured) or
   when hard budget overflow persists even after compaction, mirroring
   `ContextAssembler`'s own existing emergency-drop pattern
   (`assembler.py:127-138`). It must never run *instead of* compaction when
   compaction is available — today both can fire with no ordering; this
   document states the ordering once so it stops being ambiguous.
3. **Wire the orphaned `index_session_summary`** (§0.6): `ContextCompactor`'s
   generated summary should also be indexed there, so it becomes
   independently semantically retrievable later — a few lines of wiring,
   not new summarization logic.

## 10. Context Preservation

`ContextAssembler.UNTRIMMED_SECTIONS` (`SYSTEM_PROMPT`,
`ARCHITECTURAL_DRIFT`, `CURRENT_PROMPT`) remains the base "never remove"
list, unchanged. This document adds one preservation rule upstream of
`ContextAssembler`: the **most recent** tool result and the **most recent**
diagnostic are tagged with the same non-droppable priority as
`ARCHITECTURAL_DRIFT` before they ever reach the assembler — closing the
"current error, current tool result" gap (§0.4) at the source, not by
changing `ContextAssembler`'s own trimming rules.

## 11. Context Policies

```text
Never Remove
  • SYSTEM_PROMPT / ARCHITECTURAL_DRIFT / CURRENT_PROMPT   (existing, unchanged)
  • Most recent tool result                                 (NEW, §10)
  • Most recent diagnostic                                   (NEW, §10)

Compress
  • WORKING_MEMORY history — ContextCompactor primary, compress_conversation
    fallback-only (§9)

Discard
  • Duplicate chunks caught by cross-source fusion (§6)
  • Repository snapshot beyond its existing 2000-token reduction (unchanged,
    ContextAssembler._trim_repository_snapshot)
  • Tool results / diagnostics beyond the ToolResultSource / DiagnosticsSource
    bounded retention window (§4) — oldest dropped first, same pattern as
    WORKING_MEMORY trimming
```

## 12. Context Pipeline

```text
ContextIntelligence.gather(intent, request_text, workspace, budget, model)
    │
    ├─ asyncio.gather over registered ContextSourceProvider instances:
    │     Repository (HybridRetriever, unchanged)
    │     Memory + Knowledge Graph (ThreeBrainCoordinator, unchanged)
    │     Conversation (working-memory replay, unchanged)
    │     Git (GitTracker, promoted — §4)
    │     Tool Results (NEW ToolResultSource)
    │     Diagnostics (NEW DiagnosticsSource)
    │
    ├─ ContextFuser.dedupe(all_chunks)                (§6 — extends existing algorithm)
    ├─ ContextRanker.rank(deduped_chunks, intent)       (§7 — extends existing formula)
    ├─ AdaptiveBudgetAllocator.bias_for(intent_type)      (§8 — formalizes existing table)
    ├─ CompressionPolicy.apply_if_needed(...)              (§9 — resolves precedence)
    ├─ PreservationTagger.tag(chunks)                       (§10 — tags never-remove)
    │
    └─ returns list[ContextChunk]
              │
              ▼
       ContextAssembler.assemble(chunks, budget, model)   (existing, UNCHANGED, §0.8)
              │
              ▼
       Prompt Intelligence's ContextBuilder                  (existing, UNCHANGED, §0.8)
```

## 13. Public APIs

```python
# velune/context_intelligence/__init__.py

async def gather_context(
    intent_type: IntentType,          # existing enum, velune.cognition.intent
    request_text: str,
    workspace: Path,
    budget: ContextBudget,            # existing, unchanged
    model: ModelDescriptor | None = None,
) -> tuple[list[ContextChunk], GatherReport]:
    """Replaces build_turn_context's inline 4-source gather with one named
    entrypoint. Returns the fused/deduped/ranked/budget-biased chunk list
    ContextAssembler already consumes today — does not call ContextAssembler
    itself (§0.8)."""

def register_source(name: str, source: ContextSourceProvider) -> None:
    """Extensibility hook — add a new source without touching the gather loop."""

def register_bias(intent_type: IntentType, bias: float) -> None:
    """Extensibility hook — see §8."""
```

## 14. Internal APIs

```python
class ContextSourceProvider(Protocol):
    async def fetch(self, request_text: str, workspace: Path) -> list[ContextChunk]: ...
    # Existing sources (_retrieve_hybrid, _retrieve_via_memory_lifecycle,
    # _lineage_chunk, _repository_snapshot_chunks) are wrapped as instances of
    # this protocol unchanged — this is an adapter, not a rewrite.

class ContextFuser:
    def dedupe(self, chunks: list[ContextChunk]) -> list[ContextChunk]: ...
        # Same content-key algorithm as HeuristicReranker._deduplicate_by_content,
        # scope extended per §6.

class ContextRanker:
    def rank(self, chunks: list[ContextChunk], intent_type: IntentType) -> list[ContextChunk]: ...
        # Wraps HeuristicReranker._calculate_combined_score unchanged, plus the
        # optional execution_importance dimension (§7).

class AdaptiveBudgetAllocator:
    def bias_for(self, intent_type: IntentType) -> float: ...
    def register_bias(self, intent_type: IntentType, bias: float) -> None: ...

class CompressionPolicy:
    def apply_if_needed(self, budget: ContextBudget) -> None: ...
        # Calls ContextCompactor first, compress_conversation only as fallback (§9).
```

## 15. Data Models

```python
@dataclass(frozen=True)
class GatherReport:
    sources_queried: list[str]
    sources_failed: list[str]         # per-source try/except, existing pattern (§18)
    chunks_gathered: int
    chunks_deduplicated: int
    compression_applied: str | None   # "compactor" | "truncation" | None
    execution_importance_used: bool
```

No new chunk type is introduced — `ContextChunk` (`velune/context/sections.py`,
existing) remains the single currency this subsystem produces and
`ContextAssembler` consumes; inventing a parallel wrapper type here would
violate this document's own "one owner per responsibility" standard.

## 16. Class Diagram

```text
                    ┌───────────────────────┐
                    │   ContextIntelligence    │  NEW — thin coordinator
                    └───────────┬─────────────┘
                                │  registers/calls
        ┌───────────────────────┼───────────────────────────┐
        ▼                       ▼                             ▼
┌───────────────┐   ┌─────────────────────┐        ┌────────────────────┐
│ HybridRetriever │   │ ThreeBrainCoordinator │        │  GitTracker           │
│ (existing)       │   │  (existing)             │        │  (existing, promoted   │
└───────────────┘   └─────────────────────┘        │   to first-class source)│
                                                       └────────────────────┘
        ┌───────────────┐        ┌───────────────────┐
        │ ToolResultSource│        │ DiagnosticsSource    │   both NEW
        └───────────────┘        └───────────────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │      ContextFuser        │  NEW — extends
                    │  (extends existing dedup)  │  HeuristicReranker's algorithm
                    └───────────┬─────────────┘
                                ▼
                    ┌───────────────────────┐
                    │      ContextRanker        │  NEW — wraps existing
                    │  (wraps HeuristicReranker)  │  HeuristicReranker formula
                    └───────────┬─────────────┘
                                ▼
                    ┌───────────────────────┐
                    │ AdaptiveBudgetAllocator   │  NEW — formalizes existing
                    │  (formalizes existing table)│  _RETRIEVAL_BUDGET_BIAS
                    └───────────┬─────────────┘
                                ▼
                    ┌───────────────────────┐
                    │    CompressionPolicy       │  NEW — orders existing
                    │ (orders ContextCompactor /   │  ContextCompactor +
                    │  compress_conversation)       │  compress_conversation
                    └───────────┬─────────────┘
                                ▼
                        list[ContextChunk]
                                │
                                ▼
                 ContextAssembler.assemble()        EXISTING, UNCHANGED
                                │
                                ▼
              Prompt Intelligence's ContextBuilder     EXISTING, UNCHANGED
```

## 17. Sequence Diagram

```text
USER SUBMITS TURN
│
├─ CognitiveCore.handle_turn()'s Gather Context step   (existing, per
│                                                         03-cognitive-architecture.md §6)
│
├─ ContextIntelligence.gather(intent_type, text, workspace, budget, model)  ← NEW
│     │
│     ├─ asyncio.gather over registered sources (§12) — SAME concurrency
│     │     shape as today's build_turn_context, just named and extended
│     │     with Git/ToolResults/Diagnostics
│     │
│     ├─ ContextFuser.dedupe(...)           — NEW scope, existing algorithm
│     ├─ ContextRanker.rank(...)             — existing formula + optional
│     │                                         execution_importance
│     ├─ AdaptiveBudgetAllocator.bias_for(...) — existing table, formalized
│     ├─ CompressionPolicy.apply_if_needed(...) — existing mechanisms, ordered
│     │
│     └─ returns (list[ContextChunk], GatherReport)
│
├─ ContextAssembler.assemble(chunks, budget, model)   (existing, UNCHANGED)
│
├─ Prompt Intelligence's ContextBuilder.build(...)      (existing, UNCHANGED,
│                                                          per 02-prompt-intelligence.md)
│
└─ ...rest of the turn, unchanged
```

## 18. Failure Modes

| Failure | Behavior |
|---|---|
| A registered source times out or throws | Same per-source try/except pattern `build_turn_context` already uses today (each `_retrieve_*` function returns `[]` on error) — one slow/broken source never blocks the others |
| `ContextFuser` dedup produces a content-key collision between two genuinely different chunks | Same accepted risk `HeuristicReranker`'s existing narrower dedup already carries today — not a new risk introduced by widening its scope |
| `execution_importance` computation fails (blast-radius unavailable) | Ranking falls back to the existing 3-factor formula unchanged — the 4th dimension is additive and optional, never a hard dependency (§7) |
| `ContextCompactor` unavailable (no summarization model configured) | Falls to `compress_conversation` truncation, exactly as designed (§9) — not a failure, the documented fallback path |
| `ToolResultSource` / `DiagnosticsSource` grow unbounded | Bounded ring-buffer retention, oldest dropped first — same pattern `WORKING_MEMORY` trimming already uses |

## 19. Security

- `ToolResultSource` and `DiagnosticsSource` content passes through the same
  `CognitiveFirewall` trust-wrapping repository content already gets
  ([02-prompt-intelligence.md](02-prompt-intelligence.md) §16) — tool output
  can contain untrusted file content and must never be treated as more
  trustworthy than its origin.
- `ContextFuser`'s merge-on-collision rule keeps the **original** highest
  trust score among a colliding set — it must never average or boost trust
  upward just because multiple sources happened to surface the same content;
  doing so would let a low-trust source "launder" itself by echoing
  high-trust content.
- `execution_importance` must remain a pure function of structural graph
  facts (fan-in counts via `estimate_blast_radius`), never influenced by a
  file's own content — mirrors
  [03-cognitive-architecture.md](03-cognitive-architecture.md) §16's rule
  that tier classification must not be manipulable by untrusted repository
  content; here, a file must not be able to inflate its own ranked
  importance by claiming significance in its own text.

## 20. Performance

- `ContextIntelligence.gather()` reuses `build_turn_context`'s existing
  `asyncio.gather` concurrency shape — adding Git/Tool-Results/Diagnostics as
  two or three more concurrent sources does not introduce a new latency
  class, just more branches of the same fan-out.
- Widening `ContextFuser`'s dedup scope from 3 arms to all sources is still
  O(n) with a hash-set keyed on a 100-character prefix — same complexity
  class as today's narrower version.
- `execution_importance` reuses `estimate_blast_radius`'s existing
  TTL-cached volatility lookups (600s cache, `graph_patcher.py`) — no new
  expensive graph traversal, reuses a cache the Council already keeps warm.

## 21. Migration Plan

- **Phase 0 is name-and-wrap only.** `ContextIntelligence.gather()` calls
  the exact same four source functions `build_turn_context` already calls,
  in the same order and concurrency, with zero behavior change — proven via
  parity test before any extension lands.
- Only after parity is proven do the five enumerated gaps (§0.3-§0.7) get
  closed, one at a time: widen dedup scope, add Tool Results/Diagnostics
  sources, add the execution-importance ranking dimension, formalize the
  budget allocator, resolve compression precedence.
- **Blast radius:** touches `prompt_context.py`'s call site (replacing the
  inline gather with `ContextIntelligence.gather()`). Does **not** touch
  `ContextAssembler`, `ThreeBrainCoordinator`, `RepositoryCognitionService`,
  `HybridRetriever`, `RetrievalPlanner`, or `ContextCompactor` internals —
  only wraps and, where explicitly noted, extends them.

## 22. Implementation Phases

1. **`ContextIntelligence` skeleton** wrapping today's four sources
   unchanged — parity test against current `build_turn_context` output.
2. **`ContextFuser`** — widen `HeuristicReranker`'s dedup scope across all
   sources (§6).
3. **`AdaptiveBudgetAllocator`** — formalize `_RETRIEVAL_BUDGET_BIAS` as a
   registered policy (§8).
4. **`ToolResultSource`** — bounded retention, `CognitiveFirewall`-wrapped
   (§0.4, §19).
5. **`DiagnosticsSource`** — reuse the existing `PythonLinter` invocation,
   bounded retention.
6. **Execution-importance ranking dimension** — reuse
   `estimate_blast_radius` unchanged, gated behind a config flag (§7).
7. **Resolve compression precedence** (§9) — `ContextCompactor` primary,
   `compress_conversation` fallback-only; wire the orphaned
   `index_session_summary` (§0.6).
8. **Promote Git to a first-class source** (§4) — same `GitTracker` data,
   now its own ranked/deduped chunk instead of buried snapshot text.
9. Hand off to **`07-repository-intelligence.md`** as the next design
   document, per the established roadmap order.

---

*Assumptions made explicit for review: (a) `execution_importance`'s additive-
boost weighting (rather than a full re-weighting of the tuned 3-factor
formula) is a starting assumption that should be checked empirically once
Phase 6 ships — if it doesn't measurably improve outcomes, the config flag
(§7) exists specifically so it can be turned off without a redesign; (b) this
document assumes `ToolResultSource`'s bounded ring-buffer is in-session only
(not a new persistent memory tier) — if tool-result recall across sessions
turns out to matter, that's a Knowledge Brain / Knowledge Layer extension,
not something to retrofit here; (c) Workspace-as-a-source (open files,
terminal state) is deliberately left unbuilt (§4) — naming it as a future
extension point kept this document from expanding scope speculatively ahead
of a concrete need.*
