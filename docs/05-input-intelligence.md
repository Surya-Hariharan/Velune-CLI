# Input Intelligence: Architecture Design

*The sixth architectural pillar. Where [Prompt Intelligence](02-prompt-intelligence.md)
compiles what Velune says and [Cognitive Architecture](03-cognitive-architecture.md)
decides what Velune does, Input Intelligence is the concrete design for the
first thing that happens to a turn, before either of those run: turning noisy,
misspelled, ambiguous, or mislabeled user input into a normalized request the
rest of the system can reason about. No system prompt can fix a typo or
disambiguate which "Claude" a user meant — that is a different layer, and
this document is that layer. It is a design document; no code changes
accompany it.*

---

## Contents

- [0. Current State & Gap Analysis](#0-current-state--gap-analysis)
- [1. Purpose](#1-purpose)
- [2. Design Philosophy & Alternatives Considered](#2-design-philosophy--alternatives-considered)
- [3. The Three-Tier Correction Policy](#3-the-three-tier-correction-policy)
- [4. Responsibilities](#4-responsibilities)
- [5. Pipeline](#5-pipeline)
- [6. Component Responsibilities](#6-component-responsibilities)
- [7. Public Interfaces](#7-public-interfaces)
- [8. Internal Interfaces](#8-internal-interfaces)
- [9. Data Models](#9-data-models)
- [10. Class Diagram](#10-class-diagram)
- [11. Sequence Diagram](#11-sequence-diagram)
- [12. Failure Modes](#12-failure-modes)
- [13. Security Considerations](#13-security-considerations)
- [14. Performance Considerations](#14-performance-considerations)
- [15. Migration Plan](#15-migration-plan)
- [16. Implementation Phases](#16-implementation-phases)

---

## 0. Current State & Gap Analysis

Verified against source. The headline finding: Velune already has *fuzzy
matching* in four places, and all four are the same algorithm solving a
different problem than the one this document is about.

### 0.1 Existing fuzzy matching is UI-filtering, not input correction

`velune/cli/autocomplete.py:101-129`'s `fuzzy_score()` is a tiered
exact(1000) > prefix(500) > substring(250) > in-order-subsequence(1-100)
matcher, reused verbatim by the command palette's picker
(`command_palette.py`), the model picker (`handlers/model.py:173-179`), and
generic list widgets (`interactive/widgets/select.py:76`,
`dir_browser.py:133`). It is genuinely useful for *as-you-type filtering of
a visible list* — but it is a subsequence matcher, not an edit-distance
matcher, and it is **never consulted for a fully-typed, already-submitted**
command or name. Verified empirically: `fuzzy_score("porviders",
"providers")` → `0`, `fuzzy_score("gimini", "gemini")` → `0`,
`fuzzy_score("modle", "model")` → `0` — every one of these is a
transposition, and transpositions defeat an in-order-subsequence matcher by
construction. A user who types `/porviders` (not `/prov` then browses a
list) gets no correction anywhere in the system.

### 0.2 Two mechanisms are labeled "fuzzy" in comments but are exact-match

`velune/context/mentions.py:124-149` (`_resolve_path`, step 2, commented
"Basename fuzzy match") and `velune/providers/local_resolver.py:113-142`
(GGUF file resolution) both do an **exact** filename/stem string comparison
(`path.name == target_name`, `mentions.py:137`) across the workspace tree —
not fuzzy in any real sense. This is a pre-existing, harmless naming
inaccuracy in code comments (not a functional bug); flagged here because it
matters for accurately scoping this document's actual gap, not as something
to fix in passing.

### 0.3 Unresolved @mentions are silently dropped

`parse_mentions()` returns an `unresolved` list for any `@token` that didn't
resolve to a real file (`mentions.py:82,92,110`). Its only caller,
`velune/cli/repl.py:1285`, assigns it to `_unresolved` and never reads it
again — no warning, no "did you mean," nothing reaches the user. This is the
single clearest concrete instance of the gap this document exists to close:
a user typing `@promt_context.py` (missing an `p`) gets silent nothing,
where the correct behavior is either a high-confidence auto-resolution (if
exactly one file in the repo is an edit-distance-1 match) or a low-confidence
"did you mean `@prompt_context.py`?" (§3).

### 0.4 No alias/name-resolution layer for models or providers

`/model <name>` resolves through `ModelCapabilityRegistry.get()`
(`velune/models/registry.py:155-169`) — dict lookup, then a linear exact-
equality scan (line 167), raising `ModelNotFoundError` with no suggestion on
miss. `provider_ui.py`'s `/connect` flow is menu-selection only. **"claude"
typed directly does not resolve to a concrete model id anywhere** — the only
place name-fuzziness exists at all is the picker UI shown when `/model` is
invoked with *no* argument (`handlers/model.py:147-247`), which doesn't help
a user who typed a full (mis-aliased or misspelled) name directly. This is
exactly the "Use Claude — which Claude?" gap from the motivating examples.

### 0.5 Symbol-mention resolution is wired but dead

`autocomplete.py` already contains `_complete_symbol_mentions` (lines
216-227) using `fuzzy_score` against `self._symbol_names`, and a
`set_symbol_names()` setter (lines 185-187) to populate it. **Nothing ever
calls `set_symbol_names()`** — `SlashCompleter` is constructed without a
`symbol_names=` argument at its one call site (`velune/cli/repl.py:231-235`),
so `_symbol_names` is permanently `[]`. This is a real, previously-built,
currently-orphaned feature — not a gap to fill from scratch, but a wire to
reconnect (§15), consistent with this codebase's recurring pattern of
built-but-never-wired subsystems (see prior audits in
[03-cognitive-architecture.md](03-cognitive-architecture.md) and
[04-knowledge-layer.md](04-knowledge-layer.md)).

### 0.6 No disambiguation ("ask the user") mechanism exists in the live turn path

The only precedent anywhere in the codebase for surfacing multiple
candidates is the standalone `velune workspace graph --focus <path>` Typer
command: `_resolve_focus()` (`velune/observability/workspace_graph.py:112-131`)
computes up to 20 `focus_candidates` on ambiguity, and
`cli/commands/workspace.py:403-407` prints them and **returns** — it does not
loop back and re-ask; the user must manually re-invoke with a disambiguated
path. Nothing in `cognition/orchestrator.py` or the REPL turn path pauses for
clarification. The closest *structural* precedent for "pause mid-flow and let
the user pick" is the provider-connect retry/save-anyway/cancel submenu
(`provider_ui.py`, per
[01-provider-management-v2.md](01-provider-management-v2.md)) — a real,
working interactive-choice pattern this document reuses rather than inventing
a new one (§6, §15).

### 0.7 IntentClassifier has zero true typo tolerance — with one correction to the motivating premise

`IntentClassifier._score()` (`velune/cognition/intent.py:317-348`) supports
`exact_prefix`, `word_boundary` (regex `\b...\b`), and plain-substring modes
— none use edit distance. Re-checking the motivating example ("anylse the
repo and finnd all buggs in promt intellgence") against the actual keyword
tables: `_DEBUG_KEYWORDS` is scored with `word_boundary=False`
(`intent.py:302`), so `"bug" in "buggs"` is `True` as a **substring
coincidence** — not evidence of typo tolerance. "anylse" (vs. `_REVIEW_
KEYWORDS`'s "analyse", word-boundary mode), "finnd" (vs. `_SEARCH_KEYWORDS`'s
"find", `exact_prefix` mode), "promt", and "intellgence" all score zero
everywhere — real transposition/deletion typos are not caught by any
existing path. The user's premise is directionally correct; the one keyword
hit in their own example is luck, not a mechanism, which if anything
strengthens the case for this document rather than weakening it.

### 0.8 Reconciling with Cognitive Architecture's existing "Perception" phase

[03-cognitive-architecture.md](03-cognitive-architecture.md) §3/§6 already
names a `Perception` / `Observe` step as the first stage of every turn,
today implemented as "existing mention-resolution (repl.py, unchanged)."
**This document is the concrete design for that existing, named,
under-specified phase — it is not a new pipeline stage inserted in front of
Cognitive Architecture**, as a literal reading of the pipeline diagram in the
originating request might suggest. Positioning Input Intelligence as a
sibling stage *before* `CognitiveCore.handle_turn()` would create two
competing owners of "the first thing that happens to a turn." Instead,
`CognitiveCore`'s `Observe` step (§11 of this document) calls into Input
Intelligence directly — the pipeline gains a real implementation, not a new
box on the diagram.

---

## 1. Purpose

Input Intelligence turns raw, possibly noisy user input — misspellings,
grammar errors, ambiguous references, mislabeled entities — into a
`NormalizedInput` that downstream systems (`IntentClassifier`, the Council's
tier classifier, `PromptCompiler`) can consume without each having to grow
its own typo/ambiguity tolerance. It is deliberately narrow: it does not
classify intent (that stays `IntentClassifier`'s job, per
[03-cognitive-architecture.md](03-cognitive-architecture.md)'s "Executive
Brain" ownership), does not retrieve context, and does not compile prompts.
It only answers: *what did the user actually mean by these characters and
these @-references*, and *when is that ambiguous enough to ask rather than
guess*.

## 2. Design Philosophy & Alternatives Considered

**Core tenet: never silently rewrite the user's intent.** A correction the
user didn't see happen is a correction that can go wrong without recourse.
Every tier in §3 below either auto-applies only when the evidence is
overwhelming, resolves using context the system can point to, or stops and
asks — there is no fourth path where the system quietly guesses and hopes.

| Option | Verdict | Why |
|---|---|---|
| **A. Make the system prompt handle it** — add more instructions asking the model to interpret typos/ambiguity itself | Rejected | This is the premise the request opened with, and the current-state audit supports it: no amount of prompt wording fixes a transposition typo or resolves which of five files named `memory.py`-adjacent concepts a user meant — the model sees the same ambiguous text a human would, per-turn, with no persistent vocabulary of the project's own entities |
| **B. Rely on the existing picker-UI fuzzy filtering** (§0.1) as the fix | Rejected | It only helps when a user pauses to browse a list (`/model` with no argument); it does nothing for a fully-typed slash command, a freeform sentence, or an `@mention` — which is the majority of real input |
| **C. Route every turn through an LLM "rewrite my query" pre-pass** by default | Rejected as the default | Violates the core tenet (a silent LLM rewrite is exactly the un-auditable substitution being guarded against) and adds a model call — and its latency/cost/failure surface — to *every* turn, contradicting `IntentClassifier`'s own explicit zero-latency design constraint (`intent.py:1-6`, "keeping latency near zero so it can run on every REPL turn"). Reserved as an opt-in, low-confidence-tier fallback only (§3, §6) |
| **D. A deterministic-first, tiered pipeline** over a closed vocabulary of Velune's own known entities (provider names, model families, slash commands, memory-tier names, repository symbols/files), with disambiguation reusing the REPL's existing interactive-choice pattern, and open-vocabulary grammar recovery as an optional, skippable, non-blocking last resort | **Chosen** | Matches what's cheap and auditable (dictionary/edit-distance lookups over vocabulary Velune already has an authoritative list of — model registry, provider catalog, command table, workspace file tree) and reserves the expensive, less-auditable option (an LLM pass) for exactly the cases deterministic methods can't resolve, never as a blanket first step |

## 3. The Three-Tier Correction Policy

This is the load-bearing design decision, adopted directly from the request
that motivated this document:

```text
Tier 1 — High-confidence correction (auto-apply)
    Edit distance 1 against a CLOSED vocabulary Velune already has an
    authoritative list for: provider ids, model families, slash command
    names, memory-tier/subsystem names. "anthroipc" -> "anthropic" (edit
    distance 2, transposition) against the exact list of registered
    provider ids is safe to auto-apply because the candidate set is small,
    known, and the alternative (a ModelNotFoundError with no suggestion,
    §0.4) is strictly worse. Auto-applied corrections are still recorded
    (never silently invisible, §13) — the user can see what was corrected
    even though they weren't asked first.

Tier 2 — Recoverable ambiguity (resolve using context)
    Multiple candidates exist, but the current turn's context narrows it to
    one with high confidence: "the memory file" resolves to a single
    concrete file when exactly one memory-related document exists in the
    current project's docs/ (or, for a repo like this one, the currently
    open/most-recently-mentioned file wins a tie). Resolved silently but
    logged with the alternatives that were ruled out (§9's
    ResolutionTrace) — auditable after the fact even though not asked
    up front.

Tier 3 — Low-confidence ambiguity (ask)
    Multiple live candidates, no context signal breaks the tie, and/or the
    action is destructive or hard to reverse. "Use Claude" with Sonnet,
    Opus, and Claude Code all plausible; "delete the cache" with more than
    one cache store present (§13 — this tier is mandatory, never
    downgraded, whenever the resolved action would be destructive,
    regardless of how confident the resolver otherwise is).
```

The tier a given correction falls into is a property of the **evidence**,
not the **subsystem** — the same `EntityResolver` call can land in tier 1 for
one input and tier 3 for another, depending on how many live candidates
remain after filtering (§9's `confidence`/`candidate_count`).

## 4. Responsibilities

- Own **Spell Correction** over Velune's closed, authoritative vocabularies
  (provider ids, model ids/families, slash command names) — never over
  arbitrary English prose (§6).
- Own **Entity Resolution** for `@mentions`, `@@symbol` references, and
  bare model/provider names typed without a `/model` or `/connect` prefix.
- Own **Ambiguity Detection** — deciding which tier (§3) a given resolution
  falls into, based on candidate count, context signal strength, and
  action reversibility.
- Own **Query Normalization** — light cleanup (whitespace, obvious
  punctuation) that never changes meaning, always auto-applied.
- Own the **ClarificationPrompt** that surfaces a tier-3 ambiguity, reusing
  the REPL's existing interactive-choice UI pattern (§0.6) rather than
  inventing a new one.
- Explicitly **not** responsible for: intent classification (stays
  `IntentClassifier`), task-tier classification (stays the Council's
  `classify_task_tier`), context retrieval, or prompt compilation. Open-
  ended "grammar recovery" of full freeform sentences is scoped as an
  optional, last-resort, off-critical-path capability (§6), not a hard
  gate every turn must pass through.

## 5. Pipeline

```text
Raw user input
    │
    ▼
Query Normalization         whitespace/punctuation cleanup — always applied,
    │                        never changes meaning (Tier 1-equivalent, but
    │                        not really a "correction" so much as hygiene)
    ▼
Spell Correction             closed-vocabulary edit-distance lookup against
    │                        provider ids / model ids / slash command names
    │                        (§6) — Tier 1 when distance is small and the
    │                        candidate set is a single best match
    ▼
Entity Resolution              @mentions -> files, @@symbols -> repository
    │                          symbols, bare names -> models/providers (§6)
    ▼
Ambiguity Detection              classifies the resolution's tier (§3) —
    │                            candidate_count, context signal, and a
    │                            hardcoded escalation to Tier 3 whenever the
    │                            resolved target feeds a destructive action
    ▼
[Tier 3?] ── yes ──▶ ClarificationPrompt (blocks this turn only)
    │ no                        │
    │                            └─▶ user picks ──▶ back into the pipeline
    ▼
NormalizedInput
    │
    ▼
CognitiveCore.handle_turn()'s Observe/Understand step (existing, per
[03-cognitive-architecture.md](03-cognitive-architecture.md) §6) — feeds
IntentClassifier and classify_task_tier exactly as today, just fed cleaner
text and resolved entities instead of raw characters.
```

**Grammar Recovery** (rewriting a garbled sentence into a well-formed one,
per the "can you find why provider are crashing..." example) is
deliberately drawn *outside* this default pipeline: it is an optional stage
a caller may request explicitly for genuinely low-signal input (where
`IntentClassifier`'s confidence — already computed today, see
`classify_with_confidence`, `intent.py:265` — comes back very low), gated
behind an available local/cheap model, and skipped entirely (falling
straight through to `IntentClassifier` unchanged) when no such model is
configured or the call would add user-visible latency. It must never be a
hard dependency of the default turn path.

## 6. Component Responsibilities

```text
QueryNormalizer          Whitespace/punctuation hygiene. Pure function,
                          no vocabulary lookups, always applied.

SpellCorrector             Edit-distance lookup (stdlib difflib.
                           get_close_matches — no new dependency; grepped
                           and confirmed unused anywhere in velune/ today)
                           over CLOSED vocabularies only:
                             - provider ids (from the existing provider
                               catalog/registry, unchanged)
                             - model ids/families (ModelCapabilityRegistry,
                               unchanged)
                             - slash command names (the existing command
                               table already used by autocomplete.py and
                               command_palette.py, unchanged)
                           Never runs against open English vocabulary —
                           that is Grammar Recovery's (optional) job, not
                           this component's.

EntityResolver              Three resolvers, one per entity kind:
                             - @mention -> file: extends mentions.py's
                               existing exact-match resolution (§0.2/§0.3)
                               with an edit-distance fallback tier and,
                               critically, SURFACES the unresolved case
                               instead of the current silent drop.
                             - @@symbol -> repository symbol: REVIVES the
                               dead wiring in autocomplete.py (§0.5) by
                               actually calling set_symbol_names() from a
                               real symbol source (KnowledgeGraph node
                               labels, per
                               [04-knowledge-layer.md](04-knowledge-layer.md),
                               or the existing RepositoryGrapher) instead of
                               building a new symbol index from scratch.
                             - bare name -> model/provider: NEW alias table
                               (§9) for common shorthands ("claude" ->
                               the active/default Claude model per the
                               user's own provider config, "gpt" -> same
                               pattern for OpenAI) layered on top of the
                               existing exact-match registries, unchanged.

AmbiguityDetector             Pure decision function: given a resolution's
                              candidate list + context signal, returns a
                              Tier (§3). Hardcodes Tier 3 whenever the
                              consuming action is flagged destructive,
                              regardless of candidate_count (§13) — this
                              rule is never overridden by a confidence score.

ClarificationPrompt             Blocks only the current turn. Reuses the
                                REPL's existing interactive-choice pattern
                                (the provider-connect retry/save-anyway/
                                cancel submenu, §0.6) rather than a new UI
                                primitive — same underlying widget, a
                                different question.

GrammarRecovery (optional)       Off-critical-path. Only invoked when
                                  IntentClassifier's own confidence
                                  (already computed today) is very low AND
                                  a cheap local/cached model is available.
                                  Its output is always shown to the user
                                  as a proposed rewrite when it changes the
                                  request materially (never a silent
                                  substitution, per §2's core tenet) —
                                  the exact mechanism for "shown" reuses
                                  whatever confirmation UI the REPL uses
                                  elsewhere, not a new one.
```

## 7. Public Interfaces

```python
# velune/input_intelligence/__init__.py

def normalize_input(
    raw_text: str,
    workspace: Path,
    ask: Callable[[ClarificationRequest], Awaitable[str]],
) -> NormalizedInput:
    """Run the full pipeline (§5) over one turn's raw input.

    `ask` is the REPL's existing interactive-choice primitive (§0.6) — this
    function never constructs its own UI, it calls back into whatever the
    caller already uses for the provider-connect-style submenu.
    """

def register_alias(entity_kind: str, alias: str, canonical: str) -> None:
    """Extensibility hook — add a new shorthand without touching the resolver."""
```

## 8. Internal Interfaces

```python
class SpellCorrector:
    def correct(self, token: str, vocabulary: Sequence[str]) -> CorrectionResult: ...

class EntityResolver:
    def resolve_mention(self, token: str, workspace: Path) -> ResolutionResult: ...
    def resolve_symbol(self, token: str) -> ResolutionResult: ...
    def resolve_model_or_provider_name(self, token: str) -> ResolutionResult: ...

class AmbiguityDetector:
    def classify_tier(self, result: ResolutionResult, *, destructive: bool) -> int:
        """Returns 1, 2, or 3 per §3. `destructive=True` forces Tier 3
        unconditionally — see §13, this is a hard safety rule, not a
        confidence-weighted decision."""
```

## 9. Data Models

```python
@dataclass(frozen=True)
class CorrectionResult:
    original: str
    corrected: str | None      # None if no confident correction found
    edit_distance: int
    candidate_count: int       # how many vocabulary entries were within threshold

@dataclass(frozen=True)
class ResolutionResult:
    original: str
    candidates: list[str]      # canonical ids/paths, best-first
    confidence: float
    context_signal: str | None # what narrowed it, if Tier 2 — always populated
                                # when confidence relies on context, never left
                                # implicit (mirrors Prompt Intelligence §14's
                                # "never resolve conflicts silently" precedent)

@dataclass(frozen=True)
class ClarificationRequest:
    prompt: str
    options: list[str]

@dataclass(frozen=True)
class NormalizedInput:
    text: str                          # normalized/corrected freeform text
    resolved_mentions: list[ResolutionResult]
    resolved_symbols: list[ResolutionResult]
    corrections_applied: list[CorrectionResult]   # never silently empty-of-record —
                                                    # every auto-applied Tier-1
                                                    # correction is listed here
```

## 10. Class Diagram

```text
                    ┌─────────────────────┐
                    │   QueryNormalizer      │  NEW, trivial
                    └──────────┬────────────┘
                               ▼
                    ┌─────────────────────┐
                    │    SpellCorrector      │  NEW — stdlib difflib over
                    │  (closed vocab only)    │  existing registries/tables
                    └──────────┬────────────┘
                               ▼
                    ┌─────────────────────┐
                    │    EntityResolver      │  extends mentions.py (existing),
                    │                          │  revives autocomplete.py's dead
                    │                          │  symbol wiring (existing, §0.5),
                    │                          │  new alias table (§6)
                    └──────────┬────────────┘
                               ▼
                    ┌─────────────────────┐
                    │  AmbiguityDetector      │  NEW, pure decision function
                    └──────────┬────────────┘
                          Tier 3?
                     ┌─────────┴─────────┐
                    yes                   no
                     ▼                     │
          ┌────────────────────┐           │
          │ ClarificationPrompt  │          │
          │ (reuses existing      │          │
          │  provider_ui.py-style  │          │
          │  submenu pattern)       │          │
          └──────────┬────────────┘          │
                      └───────────┬───────────┘
                                  ▼
                       NormalizedInput
                                  │
                                  ▼
              CognitiveCore.handle_turn()'s Observe/Understand
              (existing, per 03-cognitive-architecture.md §6)
```

## 11. Sequence Diagram

```text
USER TYPES A TURN
│
├─ CognitiveCore.handle_turn(repl, text, model)   (existing, per
│                                                    03-cognitive-architecture.md)
│
├─ Observe  — NEW: calls normalize_input(text, workspace, ask) instead of
│              today's bare mention-resolution (repl.py:1285, unchanged
│              underlying file-resolution logic, extended per §6)
│     │
│     ├─ QueryNormalizer.normalize(text)
│     ├─ SpellCorrector.correct(...) for each slash-command/model/provider
│     │     token — Tier 1 auto-applies, recorded in
│     │     NormalizedInput.corrections_applied
│     ├─ EntityResolver.resolve_mention/_symbol/_model_or_provider_name(...)
│     │     for each @mention / @@symbol / bare name
│     ├─ AmbiguityDetector.classify_tier(...) per resolution
│     │
│     └─ [any Tier 3?] → ClarificationPrompt via `ask` callback → REPL's
│           existing interactive-choice UI → user picks → resolution re-run
│           with the chosen candidate fixed
│
├─ Understand — IntentClassifier.classify_with_confidence(normalized.text)
│                (existing, unchanged — now sees corrected text instead of
│                "anylse the repo and finnd all buggs")
│
└─ ...rest of CognitiveCore's Decision Loop, unchanged
     (per 03-cognitive-architecture.md §6)
```

## 12. Failure Modes

| Failure | Behavior |
|---|---|
| `SpellCorrector` finds no candidate within edit-distance threshold | Returns the original token unchanged (`CorrectionResult.corrected = None`) — never guesses past its confidence, falls through to whatever error the unresolved name already produces today (e.g. `ModelNotFoundError`) |
| `EntityResolver` finds zero candidates for an `@mention` | Surfaced to the user as "couldn't find a file matching `@promt_context.py`" — replaces today's silent drop (§0.3), does not invent a candidate |
| `AmbiguityDetector` misclassifies a destructive action as non-destructive (missing from the hardcoded list) | Fails closed only in the sense that the hardcoded list (§13) is the single source of truth for "destructive" — expanding it is a one-line addition, not a design change; this is the same fail-safe posture as `authorize_and_execute`'s existing permission gating |
| `ClarificationPrompt`'s `ask` callback is unavailable (non-interactive context, e.g. a scripted/headless invocation) | Falls back to declining the ambiguous action with an explanatory error rather than guessing — never silently picks a candidate under time pressure |
| `GrammarRecovery`'s optional model call fails or times out | Falls straight through with the original (spell-corrected, entity-resolved) text unchanged — it was never a hard dependency (§5, §14) |

## 13. Security Considerations

- **Tier 3 is mandatory, not confidence-weighted, whenever the resolved
  target feeds a destructive or hard-to-reverse action** (delete, force-push,
  overwrite) — this mirrors this project's own standing guardrail (CLAUDE.md
  §"Executing actions with care") and must never be downgraded by a high
  resolution-confidence score. A resolver being 95% sure which cache the user
  meant is not a reason to skip asking before deleting it.
- Corrected/resolved entities must never be treated as more trustworthy than
  their origin — an `@mention`-resolved file's content still passes through
  the existing `CognitiveFirewall` trust boundary
  ([02-prompt-intelligence.md](02-prompt-intelligence.md) §16) unchanged;
  Input Intelligence resolves *which* file, it does not vouch for what's
  *inside* it.
- `SpellCorrector`'s vocabularies are Velune's own closed, authoritative
  registries (provider catalog, model registry, command table) — never
  expanded from unvalidated external input, so there is no path for a
  malicious file or memory entry to inject a plausible-looking "correction"
  target.

## 14. Performance Considerations

- The default pipeline (normalize → spell-correct closed vocab → resolve
  entities → detect ambiguity) is dictionary/edit-distance work over small,
  in-memory vocabularies — no I/O beyond what `@mention` resolution already
  does today, no model calls. This preserves `IntentClassifier`'s existing
  zero-latency guarantee (§0.7) for the common case.
- `GrammarRecovery` is the only potentially-slow component and is
  deliberately gated to run only when already-cheap signals (low
  `IntentClassifier` confidence) suggest it's needed — never unconditionally.
- `ClarificationPrompt` only blocks the one turn that triggered it, exactly
  like the existing provider-connect submenu it reuses — it does not
  introduce a new class of latency, just makes an existing UI pattern do
  double duty.

## 15. Migration Plan

- **Phase 0 is additive and reconnects existing wiring rather than building
  net-new UI.** Reviving `autocomplete.py`'s dead symbol-mention completion
  (§0.5) is calling an existing, already-tested code path with a real
  argument for the first time — not new code.
- Fixing the silent `unresolved` drop (§0.3) is a small, surgical change at
  one call site (`repl.py:1285`) — surfacing a value that's already computed,
  not adding a new computation.
- The alias table (§6, "claude" → a concrete model) and closed-vocabulary
  `SpellCorrector` are genuinely new, but sit entirely upstream of
  `ModelCapabilityRegistry`/provider catalog lookups — those stay unchanged;
  Input Intelligence only decides what string gets handed to them.
- **Blast radius:** touches `velune/cli/repl.py`'s Observe step, extends
  (does not replace) `velune/context/mentions.py` and
  `velune/cli/autocomplete.py`. Does not touch `IntentClassifier`,
  `classify_task_tier`, `ModelCapabilityRegistry`'s lookup logic, or any
  provider adapter.

## 16. Implementation Phases

1. **`QueryNormalizer`** — trivial, no dependencies, ships first as pure
   hygiene with no behavior risk.
2. **Fix the silent `@mention` drop** (§0.3) — surface `unresolved` to the
   user; smallest possible change with an immediate, visible improvement.
3. **`SpellCorrector`** over the closed vocabularies (provider ids, model
   ids, slash commands) using stdlib `difflib.get_close_matches` — Tier 1
   auto-apply only, with every correction recorded (§9).
4. **Revive `@@symbol` completion** (§0.5) by wiring a real symbol source
   (`KnowledgeGraph` node labels or `RepositoryGrapher`) into
   `set_symbol_names()`.
5. **Alias table** for bare model/provider shorthands ("claude", "gpt") —
   §6, layered on existing exact-match registries.
6. **`AmbiguityDetector` + `ClarificationPrompt`**, reusing the existing
   provider-connect submenu pattern — ships with the hardcoded
   destructive-action escalation (§13) from day one, not as a follow-up.
7. **`GrammarRecovery`** (optional, off-critical-path) — last, and only once
   the deterministic tiers above are proven, since it is the one component
   whose output isn't fully deterministic and needs the most scrutiny before
   it touches real turns.
8. Hand off to **`06-context-intelligence.md`** as the next design document
   (renumbered from `05-` — this document took that slot in the roadmap,
   per the reconciliation in §0.8).

---

*Assumptions made explicit for review: (a) this document does not propose a
new UI widget for clarification — it assumes the provider-connect submenu
pattern is generalizable, and that assumption should be checked against the
actual widget's reusability before Phase 6 begins; (b) `GrammarRecovery`'s
"cheap local/cached model" assumption depends on a local model being
configured — for users with cloud-only providers, this stage should degrade
to a no-op rather than incurring a paid API call per low-confidence turn,
and that degradation rule should be confirmed, not assumed, when Phase 7 is
implemented; (c) the alias table (§6) starts with a small, hand-curated set
("claude", "gpt", "gemini" and their common misspellings) rather than trying
to enumerate every possible shorthand up front — expanding it is cheap and
should be driven by real misses, not speculative coverage.*
