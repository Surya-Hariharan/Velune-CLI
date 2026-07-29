# Cognitive Architecture: How Velune Thinks

*The fourth architectural pillar, and the control plane over the first
three — [Provider Management](01-provider-management-v2.md) (how Velune talks to
models), Memory Management (what Velune remembers), and
[Prompt Intelligence](02-prompt-intelligence.md) (how Velune compiles what it
says). This document does not answer how Velune prompts, stores, or
traverses — it answers how Velune decides. It is a design document; no code
changes accompany it.*

---

## Contents

- [0. Current State & Gap Analysis](#0-current-state--gap-analysis)
- [1. Purpose](#1-purpose)
- [2. Design Philosophy & Alternatives Considered](#2-design-philosophy--alternatives-considered)
- [3. Cognitive Layers](#3-cognitive-layers)
- [4. Brain / Domain Responsibilities](#4-brain--domain-responsibilities)
- [5. Information Flow](#5-information-flow)
- [6. Decision Loop](#6-decision-loop)
- [7. Ownership Matrix](#7-ownership-matrix)
- [8. Component Responsibilities](#8-component-responsibilities)
- [9. Public Interfaces](#9-public-interfaces)
- [10. Internal Interfaces](#10-internal-interfaces)
- [11. Data Models](#11-data-models)
- [12. Class Diagram](#12-class-diagram)
- [13. Sequence Diagram](#13-sequence-diagram)
- [14. Extension Points](#14-extension-points)
- [15. Failure Modes](#15-failure-modes)
- [16. Security Considerations](#16-security-considerations)
- [17. Performance Considerations](#17-performance-considerations)
- [18. Migration Plan](#18-migration-plan)
- [19. Implementation Phases](#19-implementation-phases)

---

## 0. Current State & Gap Analysis

This section is the load-bearing part of the document. Everything below it is
a proposal; this part is a factual account of what exists today, verified
against source (not prior design memos, which turned out to disagree with the
code in at least one place — see §0.3).

### 0.1 "Three Brain" already means something narrower than proposed

`ThreeBrainCoordinator` (`velune/memory/three_brain.py:138-296`) is real and
live, but it is exclusively a **memory-tier query fan-out** — Hot/Working,
Warm/Semantic, Cold/Episodic (three_brain.py:9-11) — fanned out concurrently
via `asyncio.gather` (`query()`, three_brain.py:184-219) and returned as a
`ThreeBrainResult`. An optional `KnowledgeQuery` augments the warm brain with
code-graph context (three_brain.py:13) — it is a bolt-on, not a peer.

**This means two of the three proposed "brains" already have a name collision
worth resolving explicitly, not glossing over:**

- The proposed **Knowledge Brain** maps directly onto `ThreeBrainCoordinator`
  plus `KnowledgeGraph`/`KnowledgeQuery` plus `LineageMemoryTier` — no
  collision, this document uses "Knowledge Brain" as the domain label and
  documents `ThreeBrainCoordinator` as its concrete engine.
- The proposed **Repository Brain**, however, collides with existing shipped
  usage: `velune/memory/lifecycle.py:551` and
  `velune/cli/handlers/prompt_context.py:426` both already say
  `# Repository Brain: code-graph context, lowest-trust source` — a comment
  naming *one specific low-trust context channel inside the memory fan-out*,
  not a cognitive domain. `velune/cli/repl.py:1330` echoes the same phrase.
  **This document deliberately uses "Repository Cognition" instead** (which
  also matches the real class name `RepositoryCognitionService` —
  `velune/repository/cognition.py`), and flags the existing three comments as
  a candidate follow-up rename so "Repository Brain" stops meaning two
  different things in the same codebase. That rename is out of scope here
  (Tier 1 mechanical, noted not fixed).

### 0.2 There is no single decision loop today — there are two, and they don't talk to each other

This is the most consequential finding, and the actual reason a control
plane is needed. For an ordinary REPL turn, `VeluneREPL._handle_prompt`
(`velune/cli/repl.py:1332-1374`) does:

```text
build_turn_context()          → assembled context (ContextAssembler, unchanged)
run_tool_chat()  ALWAYS FIRST → native ToolLoopRunner path (tool_chat.py)
  [if unsupported/disabled]   → legacy streaming path
```

`CouncilOrchestrator` — the tiered Planner→Coder→Reviewer→Debate→Arbitration→
Synthesis pipeline with real `CouncilTier` classification
(`velune/cognition/council/tiers.py:6-10`) — is **never invoked from this
path**. It is only reachable via the explicit `/council` slash command
(`cmd_council`, `velune/cli/handlers/council.py:25`) or the MCP server
surface (`velune/mcp/server.py:176`). Concretely:

- An ordinary turn gets **no tier classification, no explicit planning
  phase, no debate/arbitration, and no lineage write** — it goes straight to
  tool-calling or plain chat.
- A `/council`-invoked turn gets the full deliberation pipeline, but the user
  has to know to ask for it; nothing routes a turn there automatically based
  on its actual complexity.
- **`IntentClassifier`** (`velune/cognition/intent.py:254-349`, 13-value
  `IntentType`, task-kind only, **no urgency dimension** — intent.py:17-32)
  and **`classify_task_tier`** (`council/tiers.py:13-78`, keyword+hardware-TPS
  heuristics) are two entirely independent classifiers. `IntentClassifier`
  feeds only `prompt_context.py`; `classify_task_tier` feeds only
  `CouncilOrchestrator`. Neither call site knows the other's output exists.

**Correction against a prior design assumption:** the Prompt Intelligence
document (§7, `TaskIntent.urgency`) assumed intent classification already
carries a reusable "Tier" concept. It does not — `IntentType` has no urgency
field, and the actual `CouncilTier` classifier is a separate, unrelated
function. `TaskIntent.urgency` in that design is aspirational pending the
unification proposed in §6 below, not something that exists to "reuse" yet.
This document corrects that assumption rather than silently carrying it
forward.

### 0.3 Two different graphs exist, serving two different purposes

`velune/repository/cognition.py`'s `RepositoryGrapher` builds a **transient,
in-memory dependency graph** per session (backed by a JSON pipeline cache,
`.velune/pipeline_cache.json`), consumed by `CouncilOrchestrator.
estimate_blast_radius` (orchestrator.py:553-600) for tier-escalation decisions
(`tiers.py:143-171`). Separately, `velune/knowledge/graph.py`'s
`KnowledgeGraph` is a **persistent, SQLite-backed, queryable graph**
(`kg_nodes`/`kg_edges`, `neighbors()`, `subgraph()` BFS at graph.py:466-504),
stored at `~/.velune/knowledge_graph.db` and reachable only through
`KnowledgeQuery` as the optional `kg_query` input to `ThreeBrainCoordinator`.
These are not the same graph and must not be conflated in later design
documents (Knowledge Graph, Repository Intelligence) — this document treats
`RepositoryGrapher` as owned by the Repository Cognition domain and
`KnowledgeGraph` as owned by the Knowledge Brain domain.

### 0.4 Reflection is conditional and inlined, not a distinct phase

`LineageMemoryTier` (`velune/memory/tiers/lineage.py:78-417`,
`log_decision`/`log_failed_experiment`/`query_continuity_warnings`) is only
written to from inside `CouncilOrchestrator`'s synthesis method
(orchestrator.py:1519-1575), gated on `tier_level == 3` (one decision log) or
`tier_level == 4` (decision, or a failed-experiment log if objections
survived debate or arbitration required human review). **Tier 1/2 turns never
write lineage, and since ordinary turns never reach `CouncilOrchestrator` at
all (§0.2), the overwhelming majority of turns today never produce a
reflection artifact of any kind.** This is separate from `record_turn`
(`cli/repl.py`, `memory/lifecycle.py`, `memory/tiers/episodic.py`), which
unconditionally persists every turn's raw content regardless of outcome —
the two must not be conflated: turn recording is unconditional, lineage/
reflection is conditional and currently reachable only from one opt-in
surface.

### 0.5 Execution has two disconnected mechanisms, no shared dispatcher

`ToolLoopRunner` (`velune/orchestration/tool_loop.py`) executes tool calls
directly through `authorize_and_execute`, invoked straight from
`run_tool_chat` (`cli/handlers/tool_chat.py:227,249`) — it never touches
`CouncilOrchestrator`. The Council path's output (`coder_proposal`, a text
diff) is instead applied afterward via `apply_council_edits`
(`cli/handlers/council.py:404-432`) using `EditBlockApplier`/
`parse_with_fallback` from `velune/execution/edit_formats` — a diff-parse-
and-patch step, not a tool call, gated on manual user review. There is
currently no shared notion of "execute the plan" between the two surfaces.

### 0.6 Observability covers one of the two paths, and not routing decisions

`velune/observability/trace_sink.py` persists genuine phase milestones
(`[Planner]`, `[Coder]`, `[Debate]`, `[Arbitration]`, `[Synthesis]`) emitted
by `CouncilOrchestrator.stream`'s `progress_callback`
(orchestrator.py:273-297), replayable via `velune trace`. It records **only**
the Council path. It does not record which tier was chosen, which memory
brain served a hit, or anything from the far more commonly used
`ToolLoopRunner` path. There is no unified "which brain / which tier / which
path handled this turn and why" trace today — this is new work this document
scopes (§14, §19), not something already built.

### 0.7 Summary of the gap

Velune has three well-built subsystems (Provider Management, Memory
Management, Prompt Intelligence) and one well-built but siloed deliberation
engine (`CouncilOrchestrator`) that only a minority of turns ever reach. What
does not exist is the thing that makes every turn — not just `/council`
turns — go through a consistent Observe→Understand→Plan→Gather→Execute→
Evaluate→Reflect→Update sequence, with tier/complexity determining *how much*
of that sequence runs, not *whether an entirely different code path runs
instead*. That is this document's subject.

---

## 1. Purpose

Cognitive Architecture defines the **control plane**: the single place that
decides, for every turn, what kind of request this is, how much deliberation
it deserves, which existing subsystems to invoke and in what order, when to
stop, what counts as success, and what gets written back to memory. It does
not reimplement any existing subsystem — not `ContextAssembler`, not
`ThreeBrainCoordinator`, not `PromptCompiler`, not `CouncilOrchestrator`, not
`ToolLoopRunner`. It sequences them, consistently, through one traceable
decision loop, replacing the implicit branching that today lives split
across `repl.py`, `tool_chat.py`, and `council.py`.

## 2. Design Philosophy & Alternatives Considered

**Core tenet, carried forward from Prompt Intelligence (§3.3 of that
document): reuse, don't replace.** Every existing subsystem identified in §0
keeps its class names, its internals, and its current responsibilities. The
new component is a thin coordinator above them, not a rewrite of any of them.

| Option | Verdict | Why |
|---|---|---|
| **A. Leave decision logic distributed** as it is today (implicit branching in `repl.py`/`tool_chat.py`, Council reachable only via slash command) | Rejected | This is precisely the gap in §0.2 — no single place to reason about, trace, or extend "how Velune decides"; adding a fourth surface (e.g. a scheduled/background agent) would mean writing a *third* independent branching path |
| **B. Fold routing into `CouncilOrchestrator` itself** — expand it to also own tool-loop dispatch, intent classification, and repository-cognition calls | Rejected | Turns one already-large, well-tested class into a god-object; violates reuse-don't-replace; makes `CouncilOrchestrator` harder to unit-test in isolation from routing concerns it doesn't currently have |
| **C. Build the LangGraph-based graph executor now**, since that is the stated long-term target | Rejected for now | Premature relative to the user's own roadmap ordering — Graph Traversal is intentionally sequenced *after* Knowledge Graph and Repository Intelligence are fully designed; committing to a graph-execution dependency before those exist would mean redesigning the executor later anyway |
| **D. A thin coordinator (`CognitiveCore`) that sequences existing subsystems** through one explicit loop, owning only routing/sequencing/stopping-conditions | **Chosen** | Every subsystem it calls already exists and is tested; the sequencing logic itself is small enough to reason about and trace; §14 shows how it can later delegate its internal sequencing to a LangGraph executor without changing its external contract |

## 3. Cognitive Layers

```text
User Request
    │
    ▼
Perception        — turn ingestion: raw text, mentions, attachments (existing:
    │                 repl.py mention resolution, unchanged)
    ▼
Understanding      — WHAT is this and HOW MUCH does it need (NEW: unifies
    │                 IntentClassifier + classify_task_tier, §0.2/§6)
    ▼
Planning           — delegated to CouncilOrchestrator's existing Planner
    │                 phase when tier warrants it; a no-op pass-through for
    │                 INSTANT-equivalent turns (existing, reused as-is)
    ▼
Reasoning          — Coder/Reviewer/Challenger/Critics/Debate/Arbitration,
    │                 exactly as CouncilOrchestrator already implements
    │                 (existing, reused as-is; not re-run for low tiers)
    ▼
Execution          — ToolLoopRunner OR council-diff-apply, chosen by the
    │                 same routing decision Understanding made (existing
    │                 mechanisms, NEW shared entry point, §14)
    ▼
Reflection         — evaluate outcome, extend LineageMemoryTier writes to
    │                 every tier (not just 3/4) at a scope appropriate to
    │                 that tier (NEW, §6/§19)
    ▼
Learning           — record_turn (existing, unconditional, unchanged) +
                      conditional lineage write (existing mechanism, now
                      reachable from every turn instead of only /council)
```

## 4. Brain / Domain Responsibilities

### Executive Brain
```text
Owns:
- intent classification            (IntentClassifier, existing)
- tier / complexity classification  (classify_task_tier, existing)
- planning, delegation sequencing    (CouncilOrchestrator's phase gating,
                                       existing — orchestrator.py:846-865)
- stopping conditions                 (max_debate_turns bound, existing;
                                        NEW: a turn-level stopping condition
                                        that today doesn't exist because
                                        ordinary turns never enter a bounded
                                        loop at all, §0.2)
NEW responsibility not owned by anything today:
- deciding, per turn, whether this is a native-tool-loop turn or a
  Council-deliberation turn — currently an implicit fact about which code
  path repl.py happens to call, not a decision anything makes explicitly
```

### Repository Cognition
```text
Owns:
- repository understanding    (RepositoryCognitionService, existing)
- dependency analysis          (RepositoryGrapher, existing)
- project conventions           (ProjectTypeDetector/ArchitectureDetector,
                                  existing; NEW: the ProjectConventions
                                  loader proposed in Prompt Intelligence §12
                                  belongs here, not to Prompt Intelligence,
                                  once it exists)
- symbol/API reasoning            (APIMapper, existing)
Explicitly NOT the same as the memory-fan-out's "Repository Brain" context
channel (§0.1) — that channel is this domain's OUTPUT consumed by Knowledge
Brain, not this domain itself.
```

### Knowledge Brain
```text
Owns:
- working/semantic/episodic memory   (ThreeBrainCoordinator, existing)
- knowledge graph                     (KnowledgeGraph/KnowledgeQuery, existing)
- previous conversations                (episodic tier, existing)
- execution lineage                      (LineageMemoryTier, existing —
                                           NEW: written from every tier, §0.4)
- semantic retrieval                       (existing)
```

## 5. Information Flow

```text
User
  │
  ▼
CognitiveCore.handle_turn()   ← NEW single entry point, replaces the implicit
  │                              branching currently split across repl.py
  │                              lines 1332-1374 and cmd_council
  │
  ├─▶ Executive Brain: IntentClassifier + classify_task_tier (existing
  │      functions, NEW: called together, from one place, §6)
  │
  ├─▶ Repository Cognition: RepositoryCognitionService.get_snapshot_fresh()
  │      (existing, already called today — just now explicitly sequenced
  │      rather than implicitly reached via prompt_context.py)
  │
  ├─▶ Knowledge Brain: ThreeBrainCoordinator.query() (existing, unchanged)
  │
  ├─▶ Context Intelligence   [FUTURE DOC — extension point only, §14]
  │
  ├─▶ Prompt Intelligence: PromptCompiler.compile_prompt() (existing design,
  │      not yet implemented per its own migration plan)
  │
  ├─▶ Provider Management: resolves model/provider (existing, unchanged)
  │
  ├─▶ LLM (existing provider adapters, unchanged)
  │
  ├─▶ Execution: ToolLoopRunner OR council-diff-apply (existing mechanisms;
  │      NEW: chosen by CognitiveCore's routing decision instead of
  │      repl.py's current "always try native tool loop first" default)
  │
  ├─▶ Reflection: evaluate outcome against the tier's success criteria
  │      (NEW as a distinct step; LineageMemoryTier write is existing
  │      machinery reused, now invoked for every tier)
  │
  └─▶ Memory Update: record_turn (existing, unconditional, unchanged)
```

## 6. Decision Loop

```text
Observe          — ingest the raw turn (existing: repl.py mention resolution)
    │
Understand       — NEW unified step: run IntentClassifier and
    │               classify_task_tier together, producing one
    │               TurnUnderstanding{intent_type, tier, confidence} instead
    │               of two independent, uncorrelated classifications (§0.2)
    │
Plan             — if tier ∈ {STANDARD, FULL}: CouncilOrchestrator's existing
    │               Planner phase runs. If tier ∈ {INSTANT, MINIMAL}: planning
    │               is a pass-through (matches today's actual behavior for
    │               those tiers — no regression, just made explicit)
    │
Gather Context   — Repository Cognition snapshot + Knowledge Brain query +
    │               Prompt Intelligence's ContextBuilder (existing/designed
    │               mechanisms, unchanged)
    │
Execute          — ToolLoopRunner (function-calling models) or council-diff-
    │               apply (Council-tier turns without native tool support) —
    │               existing mechanisms, NEW: selected by one routing
    │               decision instead of "always native tool loop first"
    │
Evaluate         — NEW: did execution succeed against the tier's implicit
    │               success criteria (tool calls completed without
    │               ToolBlockedError / arbitration required no human review /
    │               etc.) — today nothing evaluates an ordinary turn's
    │               outcome at all; it just ends
    │
Reflect          — LineageMemoryTier.log_decision / log_failed_experiment —
    │               existing machinery (memory/tiers/lineage.py), NEW: called
    │               for every tier's Evaluate outcome, not gated to tier 3/4
    │               only (§0.4)
    │
Update Memory    — record_turn — existing, unconditional, unchanged
```

**Explicit precedence for what determines "how much loop runs":** tier
classification alone (`classify_task_tier`'s existing heuristics) governs
which of Plan/Reasoning/Evaluate/Reflect actually execute versus pass
through — same rule `CouncilOrchestrator` already applies internally
(`orchestrator.py:846-865`), now applied consistently to *every* turn instead
of only `/council`-invoked ones.

## 7. Ownership Matrix

| Responsibility | Owner | Status |
|---|---|---|
| Provider Selection | Provider Management | Existing, unchanged |
| Memory Retrieval | Knowledge Brain (`ThreeBrainCoordinator`) | Existing, unchanged |
| Prompt Compilation | Prompt Intelligence (`PromptCompiler`) | Designed, not yet implemented |
| Intent + Tier Classification | Executive Brain | Existing functions, **NEW: unified call site** |
| Task Planning | Executive Brain → `CouncilOrchestrator` Planner phase | Existing, reused |
| Repository Reasoning | Repository Cognition (`RepositoryCognitionService`) | Existing, unchanged |
| Context Fusion | Context Intelligence *(future doc)* | Not yet designed — extension point only, §14 |
| Execution routing (tool-loop vs. diff-apply) | Executive Brain → Agent Runtime *(future doc)* | **NEW** decision; mechanisms exist, router does not |
| Tool execution | `ToolLoopRunner` | Existing, unchanged |
| Diff application | `EditBlockApplier` / `apply_council_edits` | Existing, unchanged |
| Turn outcome evaluation | `CognitiveCore` Evaluate step | **NEW** — nothing evaluates ordinary-turn outcomes today |
| Reflection / lineage | Knowledge Brain (`LineageMemoryTier`) | Existing machinery, **NEW: invoked for every tier** |
| Unconditional turn persistence | Knowledge Brain (`record_turn`) | Existing, unchanged |
| Knowledge Graph storage/query | Knowledge Brain (`KnowledgeGraph`) | Existing, unchanged |
| Graph Traversal algorithms | *(future doc — sequenced after Knowledge Graph)* | Not yet designed |

## 8. Component Responsibilities

```text
CognitiveCore            NEW. Single per-turn entry point. Owns nothing but
                          sequencing: calls Understand, routes Plan/Execute
                          per tier, calls Evaluate/Reflect, delegates Memory
                          Update to existing record_turn. Contains no
                          business logic duplicated from any subsystem below.

TurnUnderstanding          NEW. The unified output of IntentClassifier +
                           classify_task_tier — a single dataclass instead of
                           two uncorrelated function calls from two files.

ExecutionRouter             NEW, minimal. Given TurnUnderstanding + model
                            capability (function-calling support, existing
                            ModelDescriptor field), returns which existing
                            execution mechanism to invoke. Contains no
                            execution logic itself — a lookup, not a runtime.
                            Full design of a unified Agent Runtime is a
                            future doc (§14); this is its narrowest possible
                            placeholder.

TurnEvaluator                NEW, minimal. Given an execution result (tool
                             invocations + stop_reason, or council synthesis
                             + arbitration outcome — both existing result
                             shapes), returns a success/failure judgment used
                             only to decide what Reflect writes.

(everything else: ContextAssembler, ThreeBrainCoordinator,
RepositoryCognitionService, PromptCompiler, CouncilOrchestrator,
ToolLoopRunner, LineageMemoryTier, record_turn — all EXISTING, all unchanged)
```

## 9. Public Interfaces

```python
# velune/cognition/core.py

class CognitiveCore:
    async def handle_turn(
        self, repl: VeluneREPL, text: str, model: ModelDescriptor,
    ) -> TurnResult:
        """Single entry point for one REPL turn. Replaces the sequence
        currently split across repl.py:1332-1374 and cmd_council. Delegates
        every step to an existing subsystem; adds only routing, evaluation,
        and unconditional-reflection sequencing."""

@dataclass(frozen=True)
class TurnUnderstanding:
    intent: IntentType             # existing enum, unchanged
    tier: CouncilTier               # existing enum, unchanged
    intent_confidence: float
    tier_rationale: str              # existing classify_task_tier signal, surfaced
```

## 10. Internal Interfaces

```python
class ExecutionRouter:
    def select(
        self, understanding: TurnUnderstanding, model: ModelDescriptor,
    ) -> ExecutionMechanism:
        """ExecutionMechanism.NATIVE_TOOL_LOOP | COUNCIL_DIFF_APPLY.
        Today's implicit default (repl.py:1353 'native tool loop first') is
        preserved as the fallback when tier is INSTANT/MINIMAL or the model
        lacks function-calling support advertised via ModelDescriptor."""

class TurnEvaluator:
    def evaluate(self, result: ToolLoopResult | CouncilSynthesis) -> TurnOutcome: ...

class ReflectionWriter:
    def write(self, understanding: TurnUnderstanding, outcome: TurnOutcome) -> None:
        """Calls LineageMemoryTier.log_decision/log_failed_experiment — the
        existing methods — for every tier, not only tier 3/4 (§0.4). For
        INSTANT/MINIMAL tiers this is a lightweight outcome tag, not a full
        decision narrative, keeping write volume proportionate."""
```

## 11. Data Models

```python
class ExecutionMechanism(StrEnum):
    NATIVE_TOOL_LOOP = "native_tool_loop"   # existing ToolLoopRunner path
    COUNCIL_DIFF_APPLY = "council_diff_apply"  # existing apply_council_edits path

@dataclass(frozen=True)
class TurnOutcome:
    success: bool
    mechanism: ExecutionMechanism
    tier: CouncilTier
    detail: str                 # human-readable, feeds ReflectionWriter

@dataclass(frozen=True)
class TurnResult:
    outcome: TurnOutcome
    understanding: TurnUnderstanding
    # the actual response content is unchanged — this wraps, not replaces,
    # today's existing return shapes from run_tool_chat / cmd_council
```

## 12. Class Diagram

```text
                    ┌─────────────────────┐
                    │     CognitiveCore      │  NEW — thin coordinator
                    └──────────┬────────────┘
                               │
        ┌──────────────────────┼──────────────────────────┐
        │                       │                            │
        ▼                       ▼                            ▼
┌───────────────┐    ┌───────────────────┐        ┌────────────────────┐
│ Executive Brain │    │ Repository Cognition│        │   Knowledge Brain    │
│  IntentClassifier│    │ RepositoryCognition-│        │ ThreeBrainCoordinator │
│  classify_task_  │    │   Service (existing) │        │  KnowledgeGraph       │
│    tier (existing)│    │ RepositoryGrapher    │        │  LineageMemoryTier    │
│  (unified via NEW │    │   (existing)          │        │   (existing, NEW:    │
│   TurnUnderstanding)│   └───────────────────┘        │    called every tier) │
└────────┬──────────┘                                  └──────────┬───────────┘
         │                                                          │
         └───────────────────────────┬──────────────────────────────┘
                                      ▼
                          ┌────────────────────────┐
                          │   Prompt Intelligence     │  (designed, §PROMPT_
                          │     PromptCompiler          │   INTELLIGENCE.md)
                          └───────────┬────────────────┘
                                      ▼
                          ┌────────────────────────┐
                          │   Provider Management      │  (existing, unchanged)
                          └───────────┬────────────────┘
                                      ▼
                          ┌────────────────────────┐
                          │      ExecutionRouter        │  NEW, minimal
                          └──────┬──────────┬───────────┘
                                 ▼            ▼
                     ┌──────────────┐  ┌──────────────────┐
                     │ ToolLoopRunner│  │ apply_council_edits│  (both existing)
                     └──────┬───────┘  └──────┬────────────┘
                            └───────┬──────────┘
                                    ▼
                          ┌────────────────────┐
                          │   TurnEvaluator       │  NEW, minimal
                          └──────────┬───────────┘
                                     ▼
                          ┌────────────────────┐
                          │   ReflectionWriter    │  NEW wrapper around
                          │  → LineageMemoryTier    │  existing lineage methods
                          └────────────────────┘
```

## 13. Sequence Diagram

```text
USER SUBMITS PROMPT
│
├─ CognitiveCore.handle_turn(repl, text, model)          ← NEW single entry
│
├─ Observe   — existing mention-resolution (repl.py, unchanged)
│
├─ Understand — TurnUnderstanding = f(IntentClassifier.classify(text),
│                                     classify_task_tier(text, repo_ctx, ...))
│               both existing functions; NEW is only that they're called
│               together and their outputs correlated into one object
│
├─ Plan      — [tier ∈ STANDARD/FULL] → CouncilOrchestrator Planner phase
│              [tier ∈ INSTANT/MINIMAL] → pass-through (matches today)
│
├─ Gather Context — RepositoryCognitionService.get_snapshot_fresh() (existing)
│                    ThreeBrainCoordinator.query() (existing)
│                    PromptCompiler.compile_prompt() (designed)
│
├─ (Provider Management resolves model/provider — existing, unchanged)
│
├─ Execute   — ExecutionRouter.select(understanding, model) →
│                [NATIVE_TOOL_LOOP] → ToolLoopRunner (existing, unchanged)
│                [COUNCIL_DIFF_APPLY] → CouncilOrchestrator Coder/Reviewer/
│                  Debate/Arbitration/Synthesis (existing) → apply_council_
│                  edits (existing)
│
├─ Evaluate  — TurnEvaluator.evaluate(result) → TurnOutcome
│
├─ Reflect   — ReflectionWriter.write(understanding, outcome) →
│                LineageMemoryTier.log_decision/log_failed_experiment
│                (existing methods, NEW: reached from every tier)
│
└─ Update Memory — record_turn (existing, unconditional, unchanged)
```

## 14. Extension Points

Per the user's own roadmap ordering, the following plug into
`CognitiveCore` — not into each other — and are named here only as ownership
boundaries. None are designed in this document:

```text
Context Intelligence   — sits between Gather Context and Prompt Intelligence;
                          fuses Repository Cognition + Knowledge Brain output
                          before PromptCompiler consumes it. NEXT document.

Repository Intelligence — deepens Repository Cognition's existing dependency
                           analysis. Documented after Context Intelligence.

Knowledge Graph         — deepens KnowledgeGraph's existing schema/query
                          surface. Documented after Repository Intelligence,
                          since Graph Traversal (next) needs a settled graph
                          to traverse — traversal designed prematurely today
                          would need a redesign once the graph itself changes.

Graph Traversal         — traversal/ranking algorithms over KnowledgeGraph.
                          Deliberately sequenced last among the graph-adjacent
                          docs for the reason above.

Planning Engine          — could eventually replace CouncilOrchestrator's
                           Planner phase; until designed, Plan (§6) continues
                           to call the existing Planner phase unchanged.

Reflection Engine         — could eventually replace ReflectionWriter's
                            direct calls to LineageMemoryTier with a richer
                            evaluation; until designed, Reflect (§6) is the
                            minimal wrapper described in §10.

Agent Runtime              — could eventually unify ToolLoopRunner and
                             council-diff-apply into one execution substrate;
                             until designed, ExecutionRouter (§10) is
                             deliberately the thinnest possible placeholder —
                             a lookup between two existing mechanisms, not a
                             new runtime.

LangGraph-based orchestration — CognitiveCore's Decision Loop (§6) is
                                sequenced imperatively today (Option D, §2).
                                Because each step is already a discrete call
                                into an existing subsystem, a future
                                implementation phase could re-express the
                                same loop as a LangGraph graph without
                                changing CognitiveCore's external contract
                                (§9) — this is a reason to keep step
                                boundaries explicit now, not a commitment to
                                adopt LangGraph in this document.
```

## 15. Failure Modes

| Failure | Behavior |
|---|---|
| `IntentClassifier` and `classify_task_tier` disagree sharply (e.g. intent=EXPLAIN but tier=FULL from an unrelated keyword match) | `TurnUnderstanding` surfaces both raw signals; `ExecutionRouter`/`Plan` key off tier alone (existing precedent — `CouncilOrchestrator` already treats tier as authoritative for phase gating), intent is advisory context only, never overridden silently |
| Model lacks function-calling support but tier routes to `NATIVE_TOOL_LOOP` | `ExecutionRouter` falls back to `COUNCIL_DIFF_APPLY` or legacy streaming — mirrors today's existing `run_tool_chat` "returns None when unsupported" contract (`repl.py:1353-1356`), unchanged |
| `TurnEvaluator` cannot determine success (ambiguous result shape) | Defaults to `success=None` (not `False`) — `ReflectionWriter` logs a lightweight "unevaluated" tag rather than fabricating a false negative into lineage |
| `ReflectionWriter` write fails (lineage store unavailable) | Non-fatal, logged — mirrors `record_turn`'s existing "never block the turn on persistence" behavior |
| A future `ExecutionRouter` misroutes a Council-tier turn into `NATIVE_TOOL_LOOP` and the model has no tools | Falls through to legacy streaming exactly as today — no new failure surface introduced, since this is the existing fallback chain, just reached via an explicit decision instead of an implicit `try/except None` |

## 16. Security Considerations

- `CognitiveCore` introduces no new trust boundary — it only sequences calls
  into subsystems that already enforce their own (Prompt Intelligence's
  `CognitiveFirewall` wrapping, existing `authorize_and_execute` permission
  gating for `ToolLoopRunner`, existing manual-review gate on
  `apply_council_edits`). It must not bypass any of these by, e.g., calling
  `ToolLoopRunner`'s internals directly instead of through
  `authorize_and_execute`.
- `TurnUnderstanding`'s tier classification must remain a pure function of
  the turn's own text/context (as `classify_task_tier` already is) — it must
  never be influenced by untrusted repository content, or a malicious file
  could downgrade its own edit to `INSTANT` tier to skip review gating.
- Reflection writes (`ReflectionWriter`) must not persist raw untrusted
  workspace content into `LineageMemoryTier` — mirrors the existing
  sanitization expectations already applied to repository logging elsewhere.

## 17. Performance Considerations

- `CognitiveCore.handle_turn` must add no new I/O beyond what
  `IntentClassifier`/`classify_task_tier` already do (both are zero-latency,
  keyword-based, no LLM calls) — the unification in §6 is a matter of
  calling two existing cheap functions from one place, not adding cost.
- `ExecutionRouter.select` and `TurnEvaluator.evaluate` are pure, synchronous,
  in-memory lookups — no measurable overhead versus today's implicit
  branching.
- `ReflectionWriter` extending lineage writes to every tier (§0.4) increases
  write volume on `LineageMemoryTier`'s SQLite-backed store; INSTANT/MINIMAL
  tiers must write a lightweight tag (not a full decision narrative) to keep
  this proportionate — an explicit constraint, not an incidental detail.

## 18. Migration Plan

- **Phase 0 lands with zero behavioral change.** `CognitiveCore.handle_turn`
  is introduced but, for its first release, its `ExecutionRouter` always
  selects `NATIVE_TOOL_LOOP` first exactly as `repl.py:1353` does today —
  parity is proven before any routing logic changes real turn behavior.
- Only after parity is proven does tier-based routing (Council-tier turns
  automatically reaching `CouncilOrchestrator` without the user typing
  `/council`) become the default, and only behind a flag initially, given how
  large a behavioral change "ordinary turns can now trigger deliberation and
  diff-review" is for existing users.
- `/council` remains a valid explicit override throughout and after migration
  — this plan extends when Council runs automatically, it does not remove
  the manual entry point.
- **Blast radius:** touches `repl.py`'s `_handle_prompt` (replaces the
  1332-1374 implicit sequence with a `CognitiveCore.handle_turn` call),
  `cli/handlers/tool_chat.py` and `cli/handlers/council.py` (both gain a
  shared caller instead of being reached independently), and
  `memory/tiers/lineage.py` callers (now reached from more places). It does
  **not** touch `ContextAssembler`, `ThreeBrainCoordinator`,
  `RepositoryCognitionService`, `CouncilOrchestrator`'s internal phases, or
  any provider adapter — all consumed unchanged.

## 19. Implementation Phases

1. **`TurnUnderstanding` + unified classifier call site** — wrap existing
   `IntentClassifier`/`classify_task_tier` behind one function; no routing
   change yet.
2. **`CognitiveCore` skeleton** with `ExecutionRouter` hardcoded to
   `NATIVE_TOOL_LOOP` (parity with today, §18 Phase 0).
3. **`TurnEvaluator` + `ReflectionWriter`**, wired only for the
   `COUNCIL_DIFF_APPLY` path first (where `LineageMemoryTier` writes already
   exist) — extending to `NATIVE_TOOL_LOOP` turns is the riskier, higher-
   volume change and comes next.
4. **Extend `ReflectionWriter` to `NATIVE_TOOL_LOOP` turns** with the
   lightweight-tag write mode (§17) — proves out write-volume assumptions
   before default-routing changes.
5. **Flag-gated tier-based `ExecutionRouter`** — Council-tier turns
   automatically route to `CouncilOrchestrator` without `/council`; dark-
   launch comparison against current behavior.
6. **Default-on** once stable; `/council` remains as an explicit override.
7. **Rename the three "Repository Brain" comments** (§0.1) to remove the
   naming collision — small, mechanical, explicitly deferred to here so it
   doesn't get bundled into a larger change.
8. Hand off to **Context Intelligence** (§14) as the next design document,
   per the established roadmap order.

---

*Assumptions made explicit for review: (a) tier classification remains the
sole authority for how much of the Decision Loop runs, per existing
`CouncilOrchestrator` precedent — this document does not propose intent type
ever overriding tier; (b) `ExecutionRouter`/`TurnEvaluator`/`ReflectionWriter`
are intentionally minimal placeholders, not the final Agent Runtime or
Reflection Engine designs, which are separate future documents; (c) the
correction in §0.2 (Prompt Intelligence's `TaskIntent.urgency` has no live
counterpart yet) should be reflected back into `02-prompt-intelligence.md` as a
follow-up note once this document is accepted, so the two documents don't
silently disagree.*
