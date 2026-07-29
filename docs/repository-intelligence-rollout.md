# Repository Intelligence: Rollout Record and Policy

**Status:** Living document — records what shipped, what's deliberately deferred, and the policy for anything still to come.
**Companions:** `docs/repository-intelligence-baseline.md` (the v1 audit this work closes findings against), `REPOSITORY_INTELLIGENCE_V2_ARCHITECTURE.md` (the architecture direction this work draws from).

---

## 1. What actually happened, versus what the v2 architecture doc assumed

The v2 architecture document was written as a from-scratch blueprint — a new signal-fusion pipeline, a new persistent understanding graph, a new retrieval layer, implicitly a parallel system that would run in shadow mode alongside v1 before a cutover.

That is not how this work was executed, deliberately. Once the baseline audit was in hand, the highest-leverage path was **incremental, in-place hardening of the existing five subsystems** (`velune/repository/`, `velune/intelligence/`, `velune/knowledge/`, `velune/context/`), not a parallel rewrite. Three reasons:

- **The baseline's own root-cause finding was a discipline problem, not an ideas problem.** Duplicated, non-reconciled classifiers (three `ProjectType`-shaped concepts) and near-zero test coverage on the heaviest heuristic surface were named as the top structural weaknesses — a second parallel system built on top of that foundation would have inherited the same duplication risk at a new layer instead of fixing it.
- **v1's existing infrastructure (SQLite knowledge graph, incremental indexer, grapher) was independently confirmed to be genuine, working engineering** (baseline §7: "not a nominal incremental label"). Replacing it with a new graph store would have meant re-solving problems (incremental patching, cycle-safe traversal, prompt-cache correctness) that were already solved.
- **Every change was small enough to verify against the full test suite before the next one landed.** 23 discrete fixes shipped as 23 separate, independently-revertible commits, each with the complete test suite green (2,140 → 2,344 tests) before starting the next. A parallel-system rewrite cannot be validated this incrementally — its cutover is a single, high-blast-radius event by construction.

The net effect is that most of what the v2 document called "Stage N" work is now *inside* the existing modules rather than beside them:

| v2 doc concept | What actually shipped | Where |
|---|---|---|
| Stage 1 (Signal Layer) claims with confidence | `Claim`/`ClaimAccumulator` — bounded weighted-sum confidence, not log-odds fusion (no labeled data yet to calibrate that) | `repository/schemas.py` |
| Stage 4 (Confidence Scoring) | Same `ClaimAccumulator`, wired into every `TechnologyDetector._from_*` method | `repository/technology_detector.py` |
| Stage 5 (Understanding Graph) confidence/provenance | Extended the *existing* SQLite `KnowledgeGraph` schema (migrated in place) rather than a new graph DB | `knowledge/schemas.py`, `knowledge/graph.py` |
| Stage 6 (Semantic Capability layer) node types | `NodeType.CAPABILITY` / `RUNTIME_ENTRYPOINT` defined; **no producer built** — see §3 | `knowledge/schemas.py` |
| Stage 9 (Notebook Understanding) | `.ipynb` discovery + execution-order-aware synthetic Python source | `repository/schemas.py`, `repository/parser.py` |
| Stage 10 (Dynamic Import Understanding) | Confidence-scored `imports_dynamic` edges, prefix expansion against the full file list | `repository/parser.py`, `repository/grapher.py` |
| Stage 11 (Framework Inference by shape) | Decorator/base-class/DI-constructor fingerprinting, folder-name-agnostic fallback | `repository/parser.py`, `repository/analyzer.py` |
| Stage 13 (Repository Evolution) | Co-change clustering, churn-weighted confidence decay, rename lineage as node metadata | `repository/tracker.py`, `intelligence/graph_patcher.py` |
| Stage 17 (Token Optimization) | Claude-family token counts no longer silently reuse GPT's tokenizer | `context/token_counter.py` |
| Stage 19 (Failure Recovery) policy | Cold-start LLM-reasoning guardrail (forward-looking — see §3) | `repository/llm_gate.py` |

The v1-vs-v2 comparison table in the architecture doc is still directionally correct about *what changed*; it is no longer correct about *how* — read "extended in place" wherever it implies "replaced by a new system."

---

## 2. Closed findings (traceable to the baseline audit)

Every item below is a completed commit on `main`, each independently tested:

1. Consolidated the three `ProjectType`-shaped detectors into one canonical path (`TechnologyDetector`) — closes baseline §8 Weakness "duplicated, non-reconciled classification logic."
2. Fixed the root-`main.py`-implies-FastAPI false positive (baseline §6.6) as a direct consequence of #1.
3. Unified the scanner/parser extension-to-language tables — closes the "two separate extension→language tables" weakness; also fixed dead `.cc`/`.cxx`/`.hpp`/`.hh` parser branches and always-`UNKNOWN` `.cs`/`.php`/`.rb`.
4. Backfilled test coverage for the six previously-untested classification modules, using a new 8-repo synthetic corpus (`tests/fixtures/repo_styles.py`) mirroring the baseline's benchmark set — closes baseline §8's test-coverage weakness.
5. Fixed the `index_state.json` lost-update race between the background engine, incremental runs, and full index runs.
6. Made `KnowledgeGraphPatcher`'s delete-then-reinsert transactionally atomic — closes baseline §8 "non-atomic knowledge-graph patch."
7. Added a per-file size ceiling (opaque-file fallback) at all three parse call sites — closes baseline §6.4, the sharpest single scalability finding.
8. Added vendor/third-party discovery exclusion and generated-file-marker detection — closes Recommendation 2.
9. Added first-class Jupyter notebook support — closes baseline §6.3.
10. Added a universal fallback symbol extractor for languages with no dedicated grammar — closes baseline §6.2 and Weakness "no generic/fallback path."
11. Added confidence-scored dynamic-import resolution — closes baseline §6.7 and Weakness "static-only import resolution."
12. Added rename detection to the incremental indexer — closes the "renames compute as delete+add" staleness gap.
13–15. Introduced the `Claim`/`ClaimAccumulator` confidence schema and wired it through `TechStack`, making `language`/`framework` genuinely multi-valued — closes baseline §6.5/§6.8 and Recommendation 4/5.
16. Added a confidence-calibration log — the prerequisite data named as needed before any fusion-weight tuning is more than a guess.
17. Replaced the fixed folder-name vocabulary with structural-shape fingerprinting as a fallback — closes baseline §6.6/§8's "fixed folder-name vocabulary" weakness.
18–19. Extended the SQLite knowledge graph with confidence/provenance columns and the missing `Capability`/`RuntimeEntrypoint`/`CALLS`/`TESTS`/`EVOLVED_FROM` types; surfaced and fixed a genuine pre-existing bug in the process (an ordinary `import os` crashed the KG patcher with a foreign-key violation).
20. Wired git-history evolution signal (co-change clustering, churn-weighted confidence decay, rename lineage).
21. Stopped silently routing Claude token counts through GPT's tokenizer.
22. Added a cold-start LLM-reasoning guardrail.

---

## 3. What's deliberately deferred, and why

Not everything the v2 document describes is built. Naming the gaps explicitly, rather than letting the schema/guardrail work imply more than it delivers:

- **No LLM-based Capability clustering exists.** `NodeType.CAPABILITY` and `RUNTIME_ENTRYPOINT` are defined in the schema (item 19) and the cold-start guardrail (item 22) is wired and ready, but nothing populates these node types yet — that requires community-detection-based symbol clustering plus a verified LLM naming pass (v2 doc Stage 6), which is a substantial standalone feature, not a mechanical fix. Building it *without* the guardrail already in place would have risked exactly the cold-start regression the guardrail now prevents by construction.
- **Framework-shape fingerprinting covers three signal types** (decorator-based routing, ORM-style base classes, DI constructors) — a deliberately bounded slice of v2 Stage 11's full "structural fingerprint library," chosen because they're the highest-value, lowest-ambiguity signals. Extending the fingerprint library to more architectural roles (middleware chains, event handlers, background job registrations) is additive follow-on work, not a redesign.
- **Co-change clusters are computed and exposed, not yet consumed.** `GitTracker.get_co_change_clusters` feeds `RepositoryCognitionService`'s summary; nothing downstream (layer classification, capability boundaries) reads it yet. It's real, tested, and available — wiring a consumer is separate, additive work.
- **The confidence-calibration log has no outcome producer.** `record_claim` is wired into every full index run; `record_outcome` has no caller, because nothing in the codebase yet generates a real "was this claim actually right" signal (a human correction, an agent action's traceable success/failure). Recording without a feedback source would mean fabricating one — the log is real infrastructure, ready for that integration when it exists.
- **Confidence fusion is a flat weighted sum, not log-odds/Bayesian.** Deliberate: the v1-vs-v2 discussion in the architecture doc names log-odds fusion as more principled but harder to calibrate, and there is no labeled ground truth yet to calibrate it against. The calibration log (item 16) is the prerequisite for ever making that upgrade a measured decision instead of a guess.

None of these are regressions or incomplete work relative to what was scoped — they are the boundary of what this pass of hardening covers.

---

## 4. Rollout policy for what comes next

For the deferred work in §3 — and for any future change of comparable size (a new retrieval-ranking model, a rewrite of the incremental indexer's diffing strategy, a change to which store backs the knowledge graph) — the policy going forward:

1. **Shadow mode before cutover.** A new signal, scorer, or store runs alongside the existing path, writing its output somewhere observable (a log, a parallel column, a `metadata` field) without yet being read by any real consumer. This is exactly the pattern `ConfidenceCalibrationLog` already establishes for confidence-weight tuning — extend it, don't invent a parallel mechanism per feature.
2. **Telemetry comparison, not a vibes-based judgment call.** Before a consumer switches over, there must be a concrete basis for "the new path is at least as good" — a calibration-log confirmation-rate comparison, a benchmark-corpus pass/fail delta against the `tests/fixtures/repo_styles.py` corpus, or equivalent. "It looks more principled" is not sufficient justification on its own (see §3's log-odds-fusion deferral for exactly this reasoning applied preemptively).
3. **Cutover is a single, reviewable commit that flips the consumer,** not a gradual namespace migration. Once flipped, the old path's *consumer* is gone immediately — there is no long-lived feature flag toggling between two live implementations.
4. **Delete the superseded code, don't deprecate it.** This was the pattern followed throughout this pass — `analyzer.py`'s duplicate `ProjectTypeDetector`/`MARKERS` table was deleted outright once `TechnologyDetector` became the single source of truth, not kept behind a flag or marked `@deprecated`. A correction to a wrong answer is worth more once it's the *only* answer a caller can get; a lingering second code path is exactly the "duplicated, non-reconciled" failure mode the baseline spent §8 warning about. The same applies to the deferred items in §3: when the Capability-clustering feature ships, anything it makes obsolete gets removed in the same change, not left running in parallel indefinitely.
5. **The 8-repo synthetic corpus is the acceptance bar.** `tests/fixtures/repo_styles.py` already encodes every failure mode the baseline benchmarked. Any future change to this pipeline should be checked against it before being considered done — a change that regresses one of these fixtures without an explicit, reasoned exception is not ready to ship.
