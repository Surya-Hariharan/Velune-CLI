# Knowledge Layer: Architecture Design

*The fifth architectural pillar, and the shared plumbing underneath the
**Knowledge Brain** and **Repository Cognition** domains defined in
[03-cognitive-architecture.md](03-cognitive-architecture.md) §4. This document
does not compete with those domains for ownership — it defines the common
shape (provenance, confidence, time, extraction, projection) that every
store already inside those domains either already follows or should grow
into. Written ahead of `06-context-intelligence.md` in the roadmap because it is
foundational plumbing those higher layers will consume, not because it
depends on them. This is a design document; no code changes accompany it.*

---

## Contents

- [0. Current State & Gap Analysis](#0-current-state--gap-analysis)
- [1. Purpose](#1-purpose)
- [2. What Is Knowledge in Velune?](#2-what-is-knowledge-in-velune)
- [3. Design Philosophy & Alternatives Considered](#3-design-philosophy--alternatives-considered)
- [4. Canonical Representation](#4-canonical-representation)
- [5. Storage Backends](#5-storage-backends)
- [6. Provenance](#6-provenance)
- [7. How Facts Evolve Over Time](#7-how-facts-evolve-over-time)
- [8. Memory vs. Repository Knowledge](#8-memory-vs-repository-knowledge)
- [9. Views: Graph, Vector, Markdown, Timeline, Repository](#9-views-graph-vector-markdown-timeline-repository)
- [10. Ownership: Extraction, Retrieval, Synchronization](#10-ownership-extraction-retrieval-synchronization)
- [11. Component Responsibilities](#11-component-responsibilities)
- [12. Public Interfaces](#12-public-interfaces)
- [13. Internal Interfaces](#13-internal-interfaces)
- [14. Data Models](#14-data-models)
- [15. Class Diagram](#15-class-diagram)
- [16. Sequence Diagram](#16-sequence-diagram)
- [17. Failure Modes](#17-failure-modes)
- [18. Security Considerations](#18-security-considerations)
- [19. Performance Considerations](#19-performance-considerations)
- [20. Migration Plan](#20-migration-plan)
- [21. Implementation Phases](#21-implementation-phases)

---

## 0. Current State & Gap Analysis

Verified against source. The headline finding: **Velune already has a
working instance of "canonical store plus views" — it just doesn't know it,
and it's scoped to one knowledge kind.**

### 0.1 The pattern already exists, once, for structural knowledge

`velune/knowledge/graph.py`'s `KnowledgeGraph` (SQLite-backed, `kg_nodes`/
`kg_edges`/`kg_meta` tables) **is** a canonical store, and
`velune/knowledge/query.py`'s `KnowledgeQuery` **is** a set of read-side
projections shaped for LLM consumption (`FileContext`, `SubgraphContext`
dataclasses with `.as_text()`). It already has real provenance
(`kg_nodes.provenance`/`kg_edges.provenance`, e.g. `["structural_parse"]`,
`["generic_fallback_regex"]`) and confidence (float, churn-decayed for FILE
nodes) columns, added via migration. This is not a green-field proposal —
this document generalizes an already-proven pattern to the rest of Velune's
knowledge, the same way Prompt Intelligence generalized the tool-schema
canonical-form pattern and Cognitive Architecture generalized nothing new but
gave existing pieces one sequencer. **What it lacks:** any temporal/history
dimension (only `updated_at`, overwritten on every upsert — no
`valid_from`/`valid_to`, no fact history), and it's scoped to code structure
only — episodic memory, semantic memory, and lineage decisions each have
their own separate store with no shared envelope.

### 0.2 Extraction already exists, automatically, heuristically — for one kind

`KnowledgeGraphPatcher` (`velune/intelligence/graph_patcher.py`) is a real,
live, automatic Knowledge Extractor: `RepositoryIntelligenceEngine` calls
`patch()` synchronously whenever `IncrementalIndexer` detects an
`IndexDelta`. Extraction is AST/regex-based (not LLM-driven), confidence-
scored (1.0 AST-verified, 0.5 generic-fallback), and edges are dropped rather
than fabricated when a target can't be resolved. **It only extracts
structural facts** (files/symbols/imports). Lineage decisions
(`LineageMemoryTier.log_decision`) are written directly by
`CouncilOrchestrator`'s synthesis method (per Cognitive Architecture §0.4) —
this is *also* extraction, in substance, but it isn't named as such and
shares no code or convention with `KnowledgeGraphPatcher`.

### 0.3 A concrete, discovered bug motivates §7

`EdgeType.EVOLVED_FROM` exists in the schema (`schemas.py:51-54`) specifically
to record rename lineage, but **no code path ever creates one**: a rename is
implemented as delete-old-node-then-insert-new-node in the same transaction,
and the foreign key constraint forbids an edge pointing at an
already-deleted target — so rename lineage is recorded only as node metadata
(`renamed_from` key) instead of a queryable edge. This is not a hypothetical
gap invented for this document; it is a real, currently-unreachable code path
that a bitemporal edge model (§7) fixes directly by superseding rather than
deleting.

### 0.4 Two vector backends already exist, correctly, for two different jobs

`SemanticMemory` (`velune/memory/tiers/semantic.py`, LanceDB, `nomic-embed-
text` via Ollama) indexes conversation turns. `CodeVectorConnection` (same
file, Qdrant, consumed by `velune/retrieval/vector.py`'s `VectorRetriever`)
indexes code/symbol content for repository search. A 2026-07-23 change
already consolidated what used to be two *competing* conversational-memory
vector tiers down to one (LanceDB); the Qdrant connection that remains is not
a leftover duplicate — it does a genuinely different job (code search, not
conversation recall) and should stay separate. **This document does not
propose merging these two** — it names them as two distinct Vector Views
over two distinct knowledge kinds (§9).

### 0.5 Synchronization is already asymmetric, and that asymmetry is correct — it's just implicit

The `KnowledgeGraph` is kept *correct* on every file change: `apply_patch`
deletes-then-reinserts an affected file's nodes/edges as one atomic
transaction, called directly from the indexing pipeline — no staleness
window. Memory (`ThreeBrainCoordinator`), by contrast, only gets a *warning
annotation*: `stale_file_count` accumulates from `repository.files_changed`
events (via the generic `CognitiveBus`, `velune/events.py` — "repository
event bus" is a role this bus plays, not a separate implementation) and must
be manually cleared (`clear_stale()`); nothing ever re-embeds a stale
memory. **This is not a bug to fix uniformly** — structural facts (KG) are
cheap to recompute exactly and must never be wrong; episodic/semantic
memories are expensive (re-embedding) and can tolerate a visible staleness
flag instead. §10.3 makes this asymmetry an explicit, named rule instead of
an accidental side effect of two subsystems evolving independently.

### 0.6 What the user's proposed knowledge-kind list actually maps to today

| Proposed kind | Status |
|---|---|
| Repository Graph | **Real** — `RepositoryGrapher`, transient in-memory, JSON-cached (`.velune/pipeline_cache.json`) |
| Temporal Graph | **Does not exist** — genuine gap, addressed in §7 |
| Memory Graph | **Misnomer today** — memory is tiered stores (working/semantic/episodic), not graph-shaped, except where `KnowledgeQuery` bolts in as `kg_context`. This document does not force it into graph shape (§3, Option A rejected) |
| Execution Graph | **Does not exist** — `CouncilOrchestrator`'s phase sequence and `ToolLoopRunner`'s call sequence aren't recorded as a DAG anywhere. Named as a future extension point (§9), not designed here |
| Conversation Graph | **Does not exist**, grepped and confirmed absent. Not designed here |
| Dependency Graph | **Real** — same as Repository Graph (`RepositoryGrapher`) |
| Git Graph | **Does not exist as a DAG.** `GitTracker` (`velune/repository/tracker.py`) is a shell-out probe (branch/status/commits/blame/co-change clusters/stash) with a TTL cache, not a queryable commit graph |
| Project Conventions | **Does not exist as ingested knowledge** — this is the same gap Prompt Intelligence §12 already named (`ProjectConventions` loader, not yet built) |
| Markdown Wiki | **Does not exist** — genuine gap, addressed as the new Markdown View (§9) |
| Embeddings / Vector Index | **Real, twice** — see §0.4 |

### 0.7 One found documentation bug, flagged not fixed

`velune/knowledge/graph.py`'s module docstring claims global storage
(`~/.velune/knowledge_graph.db`); the actual DI wiring
(`velune/knowledge/subsystems.py:17-18`) uses a per-workspace path. This is a
one-line doc fix, out of scope for this document, noted per the guardrail
against opportunistic fixes.

---

## 1. Purpose

The Knowledge Layer defines what "knowledge" means in Velune across every
subsystem that stores or recalls it, so that provenance, confidence, and
temporal validity are consistent concepts everywhere they appear — not
reinvented per store — and so that new representations (a human-readable
markdown projection, a temporal fact model) can be added as views over
existing stores rather than new competing stores. It is infrastructure, not
a domain: per Cognitive Architecture's ownership matrix (§7 of that
document), Knowledge Brain and Repository Cognition remain the owners of
*what* is known; this document defines the shared *shape* their stores
already partially follow.

## 2. What Is Knowledge in Velune?

```text
Structural knowledge     Code facts: files, symbols, imports, calls.
                          Owner: Repository Cognition. Store: KnowledgeGraph
                          (real, live). Extractor: KnowledgeGraphPatcher
                          (real, live, heuristic/AST).

Episodic knowledge        Past conversation turns, retrievable verbatim.
                          Owner: Knowledge Brain. Store: SQLite episodic tier
                          (real, live).

Semantic knowledge         Meaning-based recall of past turns.
                          Owner: Knowledge Brain. Store: SemanticMemory /
                          LanceDB (real, live).

Working knowledge           Current session's turns, ephemeral, in-process.
                          Owner: Knowledge Brain. Store: working memory tier
                          (real, live).

Procedural / lineage         Decisions made, experiments that failed, lessons
knowledge                 learned. Owner: Knowledge Brain. Store:
                          LineageMemoryTier (real, live, but conditionally
                          written — Cognitive Architecture §0.4).

Repository-state             Dependency graph, git signals (branch, churn,
knowledge                 blame, co-change), detected project type/
                          architecture. Owner: Repository Cognition. Stores:
                          RepositoryGrapher (JSON-cached), GitTracker
                          (shell-out probe, TTL-cached) — both real, live.

Code-search knowledge         Embeddings over code/symbol content for repo
                          search (distinct from conversational recall).
                          Owner: Repository Cognition. Store:
                          CodeVectorConnection / Qdrant (real, live).

Convention knowledge           The user's own project documentation (CLAUDE.md-
(not yet ingested)        equivalent, README, architecture docs). No owner
                          today — same gap Prompt Intelligence §12 names.
                          This document positions it as Repository Cognition
                          input once built, extracted the same
                          deterministic way as structural facts (§10.1),
                          not LLM-summarized by default.
```

Two dimensions cut across all of these and are this document's actual
subject: **provenance** (§6 — where did this fact come from, and how much do
we trust it) and **time** (§7 — is this fact still true, and what did it used
to be).

## 3. Design Philosophy & Alternatives Considered

**Core tenet, carried forward from Prompt Intelligence §3.3 and Cognitive
Architecture §2: reuse, don't replace.** Every store named in §2 keeps its
class name, schema, and current responsibilities. Nothing here proposes a
migration off SQLite, LanceDB, or Qdrant.

| Option | Verdict | Why |
|---|---|---|
| **A. One unified property graph for everything** — force episodic/semantic/lineage records into `KnowledgeGraph`'s node/edge model | Rejected | `kg_nodes`' schema (`file_path`, `line_start`, `line_end`) is structurally shaped for code, not conversation; LanceDB/Qdrant's ANN-optimized storage would be lost for no retrieval benefit; this is the same "fork the assembler" mistake Prompt Intelligence §3.4 already rejected, one layer down |
| **B. Leave every store as its own silo, permanently** | Rejected | Blocks a shared provenance/confidence vocabulary, blocks temporal facts (§7), blocks a human-auditable projection (§9) — the exact gaps §0 identifies |
| **C. A new heavyweight "Knowledge Service" that owns all storage directly**, migrating existing stores into it | Rejected | Violates reuse-don't-replace; every store named in §2 is real, tested, and correctly scoped to its job; a rewrite is unjustified risk for a document whose purpose is to name architecture, not force a migration |
| **D. A canonical envelope + typed knowledge-kind registry, layered across existing stores**, with targeted additive extensions only where a genuine gap exists (temporal fields on KG edges, a new markdown projection) | **Chosen** | Matches what already works (§0.1's `KnowledgeGraph`/`KnowledgeQuery` pair) generalized outward; new capabilities are additive schema/store extensions, never replacements; the markdown projection is purely generated, never hand-authored, so it can never become a second source of truth |

## 4. Canonical Representation

Not one schema replacing every store — **a common envelope of fields every
knowledge record conceptually carries**, whether its backing store already
has a column for each or needs one added:

```python
@dataclass(frozen=True)
class KnowledgeRecord:
    id: str
    kind: KnowledgeKind            # STRUCTURAL | EPISODIC | SEMANTIC |
                                    # LINEAGE | REPOSITORY_STATE | CODE_SEARCH
    subject: str                    # e.g. a file path, a turn id, a decision id
    content: dict[str, Any]          # kind-specific payload — never forced
                                     # into a single shape (Option A, rejected)
    provenance: list[str]            # already the exact shape kg_nodes uses
    confidence: float                # already the exact shape kg_nodes uses
    observed_at: datetime             # when this record's store last wrote it
    valid_from: datetime | None        # NEW — see §7; None means "current
                                        # stores that don't yet track this"
    valid_to: datetime | None           # NEW — see §7
    superseded_by: str | None            # NEW — see §7
```

`KnowledgeGraph` already natively expresses `id`, `subject`, `provenance`,
`confidence`. `valid_from`/`valid_to`/`superseded_by` are the only genuinely
new columns this document proposes (§7, scoped to edges only). Episodic,
semantic, and lineage stores do not need schema changes to participate —
`KnowledgeRecord` is a read-side envelope their existing rows can be mapped
into on the way out (§9), not a write-side requirement forced onto them.

## 5. Storage Backends

All existing, all reused as-is:

```text
SQLite     KnowledgeGraph (structural), episodic tier, LineageMemoryTier
LanceDB    SemanticMemory (conversational embeddings)
Qdrant     CodeVectorConnection (code-content embeddings)
JSON       RepositoryGrapher's transient dependency graph + pipeline cache
In-process Working memory tier (ephemeral, session-scoped)
```

**New, additive only:** a markdown directory (`.velune/knowledge/entities/
*.md`, §9) — generated files, not a queried store; nothing reads them back
as a source of truth, so they carry no consistency obligations of their own.

## 6. Provenance

`KnowledgeGraph`'s existing `provenance: list[str]` + `confidence: float`
convention (§0.1) becomes the Knowledge Layer-wide convention, generalized
rather than reinvented:

- **Structural facts** already carry it (`["structural_parse"]`, `["generic_
  fallback_regex"]`, `["dynamic_import"]`).
- **Lineage decisions** should carry the same two fields going forward
  (`log_decision`/`log_failed_experiment` calls, `memory/tiers/lineage.py`)
  — currently they don't, and adding them is a small, additive schema change,
  not a rewrite.
- **Episodic/semantic records** get a provenance tag at write time
  (`"user_turn"`, `"assistant_turn"`, `"session_summary"`) — mechanically
  cheap since `record_turn` already knows which of these it's writing.
- **Convention knowledge** (once the `ProjectConventions` loader exists,
  Prompt Intelligence §12) is provenance-tagged `["user_authored"]` at a
  fixed high confidence — it is data the user wrote themselves, not
  extracted, and should never be silently re-scored by a heuristic.

The rule that generalizes across all of these, stated once so it isn't
re-litigated per store: **provenance answers "how was this fact produced,"
confidence answers "how much should a consumer discount it" — deterministic/
AST-derived facts default high confidence, heuristic-fallback facts default
lower, and LLM-derived facts (should any extractor ever produce one) default
lowest until a human or a second signal corroborates them.**

## 7. How Facts Evolve Over Time

This is the one genuine new storage capability this document proposes, and
it is scoped narrowly: **bitemporal fields on `KnowledgeGraph` edges**
(`valid_from`, `valid_to`, `superseded_by`), not on nodes, and not on any
other store.

Why edges and not nodes: nodes (files, classes, functions) are identities —
a file either exists or it doesn't, and `KnowledgeGraphPatcher`'s existing
delete-then-reinsert-on-change model is the right tool for that (§0.5, this
document does not touch it). Edges are *facts about relationships*
("function X calls function Y," "file A was renamed from file B") and facts
are exactly the thing that should be superseded rather than silently
overwritten — which is precisely §0.3's discovered bug: `EVOLVED_FROM` exists
in the schema but can never be constructed because the old node is already
gone by the time the edge would be written.

The fix this document proposes: when `KnowledgeGraphPatcher` detects a
rename, instead of delete-old-node/insert-new-node in one transaction, it
inserts the new node, marks the old node's outgoing/incoming edges
`valid_to = now()`, and inserts one `EVOLVED_FROM` edge from new → old with
`valid_from = now()`. The old node itself can still be garbage-collected
later (nodes remain current-state-only, per above) — only the *edges*
carry history. `neighbors()`/`subgraph()` (existing `KnowledgeGraph` methods)
gain an optional `as_of: datetime | None` parameter; omitted, they behave
exactly as today (current-state query, no regression); supplied, they filter
to edges valid at that timestamp — this is the Timeline View's read path
(§9).

## 8. Memory vs. Repository Knowledge

This distinction is already correctly drawn by Cognitive Architecture's
ownership matrix (§7 of that document) and is not re-litigated here:
Knowledge Brain owns working/semantic/episodic/lineage; Repository Cognition
owns structural/dependency/git/convention knowledge. What this document adds
is that **both domains' stores are instances of the same pattern** —
typed knowledge kind, provenance, confidence, and (where applicable) time —
so a future cross-domain view (Timeline, §9) can read from both without
either domain losing ownership of its own writes. Knowledge Layer is
plumbing shared by two domains, not a third domain competing with them.

## 9. Views: Graph, Vector, Markdown, Timeline, Repository

```text
Graph View         KnowledgeQuery over KnowledgeGraph — REAL, LIVE, UNCHANGED.
                    Gains an `as_of` parameter (§7) for temporal queries;
                    default (current-state) behavior is identical to today.

Vector View (x2)    SemanticMemory/LanceDB (conversational) and
                    CodeVectorConnection/Qdrant (code search) — REAL, LIVE,
                    UNCHANGED, deliberately kept as two separate views (§0.4),
                    not merged.

Repository View     RepositoryCognitionService's snapshot (RepositoryGrapher +
                    GitTracker + ProjectTypeDetector + ArchitectureDetector) —
                    REAL, LIVE, UNCHANGED.

Timeline View       NEW. Reads LineageMemoryTier's decision/failed-experiment
                    log plus KnowledgeGraph edges filtered by `as_of` (§7)
                    into one chronological feed — "what did we decide, when,
                    and what structural facts were true at that moment."
                    Read-only; writes nothing of its own.

Markdown View       NEW. `.velune/knowledge/entities/*.md`, one file per
                    significant KnowledgeGraph node (a class, a module),
                    regenerated — never hand-written — whenever that node's
                    subgraph changes (§10.3: event-driven, best-effort,
                    allowed to lag). Purely a human-auditable projection: git-
                    diffable, greppable, reviewable in a PR, exactly the
                    Obsidian-style workflow fit the user named. It is NOT the
                    `ProjectConventions` loader (Prompt Intelligence §12) —
                    that reads the user's own existing docs INTO context;
                    this WRITES a derived doc OUT of the knowledge graph.
                    The two are complementary, not the same mechanism, and
                    must not be conflated.
```

All five views read from existing or minimally-extended stores; none of them
own writes to a store another view also reads from — this is what keeps the
canonical-store-plus-views shape from silently becoming N sources of truth.

## 10. Ownership: Extraction, Retrieval, Synchronization

### 10.1 Extraction

`KnowledgeGraphPatcher` remains the structural extractor, unchanged, gaining
only the rename-as-supersession behavior (§7). Its pattern — deterministic/
heuristic first, confidence-scored, never fabricate an edge to an unresolved
target — becomes the house style for any future extractor, including a
future `ProjectConventions` extractor (deterministic file read + provenance
tag, not LLM summarization by default) and a **named** Lineage Extractor
(the logic already inline in `CouncilOrchestrator`'s synthesis method,
Cognitive Architecture §0.4 — this document names it as an extractor role
without moving the code, since Cognitive Architecture's migration plan
already owns any change to where that logic lives).

### 10.2 Retrieval

Unchanged from Cognitive Architecture §5/§6: `ThreeBrainCoordinator`,
`KnowledgeQuery`, and `VectorRetriever` remain the retrieval call sites a
future Context Intelligence layer fans out to. This document adds Timeline
View and Markdown View as two more read paths available to that same fan-out
— it does not introduce a new retrieval funnel or change any existing call
site.

### 10.3 Synchronization

Stated explicitly, generalizing the asymmetry §0.5 already found in
production: **immediately-consistent, load-bearing knowledge is written by a
direct, synchronous pipeline call (`KnowledgeGraphPatcher.patch`, unchanged);
best-effort, human-facing projections are written by an async subscriber on
the existing `CognitiveBus`** (`repository.knowledge_graph_patched` →
regenerate the affected entity's markdown file; `repository.files_changed` →
`ThreeBrainCoordinator`'s existing staleness annotation, unchanged). The
Markdown View regenerator is a new bus subscriber following the second rule;
it is explicitly not given a synchronous call site, so a slow or failed
regeneration can never block indexing or a user's turn.

## 11. Component Responsibilities

```text
KnowledgeRecord           NEW. The read-side envelope (§4). Not a table, not
                          a store — a dataclass views map existing rows into.

TemporalEdgeExtension      NEW, minimal. valid_from/valid_to/superseded_by
                          columns + as_of-aware neighbors()/subgraph() on the
                          EXISTING KnowledgeGraph class. Not a new store.

RenameSupersessionPath      NEW, minimal. The corrected rename handling in
                          KnowledgeGraphPatcher (§7) — supersede, don't
                          delete-then-orphan-the-edge.

TimelineView                NEW. Read-only fan-in over LineageMemoryTier +
                          KnowledgeGraph(as_of=...). No writes.

MarkdownProjector             NEW. CognitiveBus subscriber; regenerates
                          .velune/knowledge/entities/*.md from KnowledgeGraph
                          + Timeline View on knowledge_graph_patched events.
                          Purely generated output — never a read source for
                          any other component (avoids the second-source-of-
                          truth trap named in §3 Option D).

(everything else: KnowledgeGraph, KnowledgeQuery, KnowledgeGraphPatcher,
ThreeBrainCoordinator, SemanticMemory, CodeVectorConnection, VectorRetriever,
RepositoryCognitionService, RepositoryGrapher, GitTracker, LineageMemoryTier,
record_turn, CognitiveBus — all EXISTING, all unchanged)
```

## 12. Public Interfaces

```python
# velune/knowledge/graph.py — additive, existing signatures unchanged

class KnowledgeGraph:
    async def neighbors(self, node_id: str, as_of: datetime | None = None) -> list[Edge]:
        """as_of=None (default): identical to today's current-state query.
        as_of=<timestamp>: filters to edges valid at that time (§7)."""

    async def subgraph(self, node_id: str, depth: int, as_of: datetime | None = None) -> Subgraph:
        """Same default-preserving contract as neighbors()."""

# velune/knowledge/timeline.py — NEW

class TimelineView:
    async def feed(self, subject: str | None = None, since: datetime | None = None) -> list[TimelineEntry]:
        """Chronological fan-in over LineageMemoryTier + KnowledgeGraph
        edge history. Read-only."""

# velune/knowledge/markdown_projector.py — NEW

class MarkdownProjector:
    async def project(self, node_id: str) -> Path:
        """Regenerates one entity's markdown file. Idempotent — always
        derived fresh from KnowledgeGraph + TimelineView, never hand-edited
        input."""
```

## 13. Internal Interfaces

```python
class KnowledgeRecordMapper(Protocol):
    """One per existing store — maps that store's native row shape into a
    KnowledgeRecord for any consumer that wants the unified envelope (e.g. a
    future Context Intelligence fan-out). Read-side only; never a write path."""
    def to_record(self, row: Any) -> KnowledgeRecord: ...

class MarkdownProjector:
    def _subscribe(self, bus: CognitiveBus) -> None:
        """Subscribes to 'repository.knowledge_graph_patched' — async,
        best-effort, per §10.3. A failed or slow projection never blocks
        the synchronous KnowledgeGraphPatcher.patch() call that triggered it."""
```

## 14. Data Models

```python
class KnowledgeKind(StrEnum):
    STRUCTURAL = "structural"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    LINEAGE = "lineage"
    REPOSITORY_STATE = "repository_state"
    CODE_SEARCH = "code_search"
    CONVENTION = "convention"          # not yet populated — no extractor exists

@dataclass(frozen=True)
class TimelineEntry:
    at: datetime
    kind: KnowledgeKind
    subject: str
    summary: str                        # e.g. lineage decision text, or
                                         # "function X calls function Y (new)"
    provenance: list[str]
    confidence: float

@dataclass(frozen=True)
class Edge:                             # existing KnowledgeGraph edge, extended
    source: str
    target: str
    edge_type: str
    weight: float
    provenance: list[str]
    confidence: float
    valid_from: datetime | None          # NEW, nullable — old rows read as None
    valid_to: datetime | None             # NEW
    superseded_by: str | None              # NEW
```

## 15. Class Diagram

```text
                         ┌─────────────────────────┐
                         │      KnowledgeGraph        │  EXISTING — gains
                         │  (SQLite, structural facts)  │  as_of param (§7)
                         └────────────┬────────────────┘
                                      │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                    ▼
          ┌──────────────┐   ┌──────────────────┐  ┌────────────────────┐
          │ KnowledgeQuery │   │  TimelineView       │  │ MarkdownProjector    │
          │  (Graph View,   │   │  (NEW — fans in also │  │  (NEW — CognitiveBus  │
          │   EXISTING)      │   │   from Lineage below) │  │   subscriber, §10.3)  │
          └──────────────┘   └─────────┬──────────┘  └──────────┬──────────┘
                                        │                          │
                                        ▼                          ▼
                              ┌──────────────────┐      .velune/knowledge/
                              │ LineageMemoryTier   │      entities/*.md
                              │  (EXISTING, memory/  │      (generated, read
                              │   tiers/lineage.py)   │       by nothing else)
                              └──────────────────┘

          ┌──────────────┐   ┌──────────────────┐
          │ SemanticMemory │   │ CodeVectorConnection│   both EXISTING,
          │  (Vector View,  │   │  (Vector View, Qdrant)│   unchanged, kept
          │   LanceDB)       │   │                        │   separate (§0.4)
          └──────────────┘   └──────────────────┘

          ┌────────────────────────────────────────────┐
          │  RepositoryCognitionService (Repository View)  │  EXISTING, unchanged
          │   RepositoryGrapher + GitTracker + detectors     │
          └────────────────────────────────────────────┘
```

## 16. Sequence Diagram

```text
FILE CHANGES ON DISK
│
├─ IncrementalIndexer detects IndexDelta            (existing, unchanged)
│
├─ RepositoryIntelligenceEngine → KnowledgeGraphPatcher.patch()  (existing,
│     synchronous, immediately-consistent per §10.3)
│     │
│     ├─ [rename detected] → NEW: insert new node, mark old node's edges
│     │     valid_to=now(), insert EVOLVED_FROM edge new→old (§7 fix,
│     │     replaces today's delete-then-orphan behavior)
│     │
│     └─ [ordinary change] → apply_patch() atomic delete+reinsert (existing,
│           unchanged — nodes remain current-state-only)
│
├─ CognitiveBus emits "repository.knowledge_graph_patched"     (existing event,
│                                                                 new subscriber)
│     │
│     ├─ ThreeBrainCoordinator (existing subscriber to files_changed,
│     │     unrelated event — staleness annotation, unchanged)
│     │
│     └─ MarkdownProjector (NEW subscriber) → regenerates the affected
│           entity's .velune/knowledge/entities/*.md — async, best-effort,
│           never blocks the patch() call above (§10.3)
│
└─ A future Context Intelligence fan-out (not designed here) can read:
      Graph View (current or as_of) | Vector Views (x2) | Repository View |
      Timeline View | Markdown files (for a human, not for retrieval)
```

## 17. Failure Modes

| Failure | Behavior |
|---|---|
| `MarkdownProjector` regeneration fails (disk full, malformed node) | Logged, non-fatal — never blocks `KnowledgeGraphPatcher.patch()`, which already committed (§10.3); the markdown file simply lags until the next successful event |
| `as_of` query requests a timestamp before any edge history was recorded (pre-migration data) | Returns current-state result (treats missing `valid_from` as "always valid") — no crash, no silent wrong answer, just reduced historical fidelity for old data |
| Rename-as-supersession path fails mid-transaction | Same atomicity guarantee `apply_patch` already provides (existing, unchanged) — the whole patch rolls back, structural correctness (§10.3's first rule) is never compromised for the sake of temporal history |
| `TimelineView` fan-in source disagrees (lineage log says one thing, KG edge history shows another) | Both are surfaced with their own provenance/confidence, not silently reconciled — a consumer (a future Reflection Engine) decides how to weigh them, per the same "never resolve conflicts silently" principle Prompt Intelligence §14 and Cognitive Architecture §15 already established |
| A `KnowledgeRecordMapper` is missing for some store a future consumer wants unified | That store simply isn't available through the unified envelope yet — existing direct access (e.g. calling `KnowledgeQuery` directly) still works exactly as today; the mapper is additive sugar, not a required migration |

## 18. Security Considerations

- The Markdown View is a **projection, never an input** — nothing reads
  `.velune/knowledge/entities/*.md` back into any store or prompt. This is
  deliberate: if it were readable input, a hand-edited or externally-planted
  markdown file could smuggle a false "fact" into the knowledge base with
  fabricated authority. Regeneration always overwrites, never merges with
  existing file content.
- `KnowledgeRecord.provenance`/`confidence` must be preserved through any
  mapper (§13) unchanged — a future Context Intelligence consumer relies on
  these to apply the same trust-boundary logic Prompt Intelligence §16
  already established for repository-derived content (never instruction-
  bearing, regardless of which view it arrived through).
- The `ProjectConventions` extractor (§10.1, not yet built) must tag its
  output `["user_authored"]` and never silently reinterpret file content as
  higher-confidence than a plain file read warrants — it is trusted because
  the user wrote it, not because an extractor scored it well.

## 19. Performance Considerations

- `as_of`-aware queries on `KnowledgeGraph` add a `WHERE valid_from <= ? AND
  (valid_to IS NULL OR valid_to > ?)` filter — indexed on `(source, target,
  valid_from)`; default (`as_of=None`) queries are unaffected and remain as
  fast as today's current-state queries.
- `MarkdownProjector` runs off the event bus, out of the request path
  entirely — it can lag indefinitely under load with zero effect on turn
  latency, by design (§10.3).
- `TimelineView` is read-only and queried on demand, not maintained
  incrementally — acceptable because lineage writes are already infrequent
  (Cognitive Architecture §0.4/§17) and KG edge history queries are bounded
  by the same indexed filter above.

## 20. Migration Plan

- **Phase 0 is additive-only and touches no existing read path.**
  `valid_from`/`valid_to`/`superseded_by` are added as nullable columns via
  migration (mirrors how `provenance`/`confidence` were themselves added,
  §0.1) — every existing row reads as `None`/"always valid," so
  `neighbors()`/`subgraph()`'s default behavior is byte-for-byte identical
  to today until `as_of` is explicitly passed.
- The rename-as-supersession fix (§7) is the first behavioral change, and it
  only activates on the rename code path specifically — ordinary file
  changes keep using `apply_patch`'s existing delete-then-reinsert, unchanged.
- `MarkdownProjector` ships and subscribes to the bus without any other
  component depending on its output — it can be disabled entirely with zero
  effect on retrieval, since nothing reads its files back (§18).
- **Blast radius:** touches `velune/knowledge/graph.py` (new columns,
  `as_of` parameter, additive), `velune/intelligence/graph_patcher.py` (rename
  handling only), and adds two new files
  (`velune/knowledge/timeline.py`, `velune/knowledge/markdown_projector.py`).
  It does **not** touch `ThreeBrainCoordinator`, `SemanticMemory`,
  `CodeVectorConnection`, `RepositoryCognitionService`, `LineageMemoryTier`'s
  write call sites, or any retrieval call site named in Cognitive
  Architecture §5.

## 21. Implementation Phases

1. **Schema migration** — add nullable `valid_from`/`valid_to`/
   `superseded_by` to `kg_edges`; parity-test that all existing queries are
   unaffected.
2. **`as_of` parameter on `neighbors()`/`subgraph()`** — defaults preserve
   current behavior; new behavior exercised only in new tests.
3. **Rename-as-supersession fix** in `KnowledgeGraphPatcher` — closes §0.3's
   dead `EVOLVED_FROM` edge type; verified against a real rename fixture.
4. **`TimelineView`** — read-only fan-in over `LineageMemoryTier` +
   `KnowledgeGraph(as_of=...)`.
5. **`MarkdownProjector`** — bus subscriber, generates
   `.velune/knowledge/entities/*.md`; ship disabled-by-default behind a flag
   until output quality is reviewed.
6. **`KnowledgeRecordMapper` per existing store** — purely additive sugar for
   a future Context Intelligence consumer; no existing call site changes.
7. **Provenance/confidence fields added to `LineageMemoryTier` writes** —
   small, additive schema change, unblocks Timeline View's confidence
   surfacing for lineage entries.
8. Hand off to **`05-context-intelligence.md`** as the next design document, now
   able to name Graph/Vector(x2)/Repository/Timeline/Markdown as the concrete
   views it fuses — followed by `REPOSITORY_INTELLIGENCE.md`,
   `KNOWLEDGE_GRAPH.md` (deepening `KnowledgeGraph`'s ontology beyond the
   structural-only node/edge types named in §0.6), and `GRAPH_TRAVERSAL.md`
   last, per the established sequencing rationale (traversal needs a settled
   graph, not the other way around).

---

*Assumptions made explicit for review: (a) bitemporal fields are scoped to
`KnowledgeGraph` edges only — this document does not propose versioning
episodic/semantic/lineage rows, since none of the discovered gaps (§0.3)
require it there; (b) the Markdown View is strictly generated output with no
read path back into any store — if a future need arises to let a human
*edit* project knowledge by hand, that is a different, not-yet-designed
mechanism (closer to the `ProjectConventions` loader's read direction) and
must not be retrofitted onto `MarkdownProjector`'s output without a
separate design pass; (c) `KnowledgeKind.CONVENTION` is defined here as a
placeholder taxonomy entry only — no extractor populates it until Prompt
Intelligence's `ProjectConventions` loader (§12 of that document) ships.*
