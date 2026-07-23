"""Turn-level context assembly for the REPL chat path.

Replaces the ad-hoc mention/retrieval/budget concatenation that used to live
inline in ``VeluneREPL._handle_prompt`` with a single call into the
canonical :class:`~velune.context.assembler.ContextAssembler`, fed by the
subsystems the architecture was designed around but which the live prompt
path never reached: :class:`~velune.cognition.intent.IntentClassifier`
(budget/priority bias), :class:`~velune.memory.three_brain.ThreeBrainCoordinator`
(working/semantic/episodic + repository knowledge-graph fan-out), and
:class:`~velune.repository.cognition.RepositoryCognitionService`'s cached
snapshot (repository structure + architectural drift).

Mentions and hook-injected system messages are handled by the caller
(``_handle_prompt``) exactly as before this module existed — they are
conversational turns, not retrieved context, so they flow into
``WORKING_MEMORY`` via ``repl._conversation`` rather than through here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from velune.cognition.intent import IntentClassifier, IntentType
from velune.context.assembler import ContextAssembler
from velune.context.budget import ContextBudget
from velune.context.sections import ContextAssemblyReport, ContextChunk, ContextSection
from velune.context.token_counter import estimate_tokens

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL
    from velune.core.types.model import ModelDescriptor

_log = logging.getLogger("velune.cli.handlers.prompt_context")

_RETRIEVAL_TIMEOUT_S = 2.0
# Larger than the retrieval budget: a warm snapshot is a cache read, but a cold
# one falls through to a full index. This bounds that worst case rather than
# letting it hold the turn open indefinitely.
_REPO_SNAPSHOT_TIMEOUT_S = 5.0

# Tier-1 subsystems the turn path degrades without. Waited on briefly so a
# prompt typed during warm-up gets real context instead of silent emptiness.
_WARM_GATED_KEYS = ("runtime.retrieval", "runtime.three_brain_coordinator")
_WARMUP_WAIT_S = 3.0
_REPO_SNAPSHOT_BASE_TOKENS = 2500
_REPO_SNAPSHOT_BIASED_TOKENS = 4000

# Intents whose turn benefits most from deep retrieval, so they use the
# session mode's full retrieval_depth rather than a half-depth default.
_DEEP_RETRIEVAL_INTENTS = frozenset({IntentType.EXPLAIN, IntentType.QUESTION, IntentType.DEBUG})
_REPO_HEAVY_INTENTS = frozenset({IntentType.REFACTOR, IntentType.REVIEW})

# How much of the retrieval/working-memory split ContextBudget gives to
# retrieval, per intent — mirrors the existing repo_snapshot_budget/depth
# bias above rather than introducing a new mechanism. Intents needing more
# retrieved material get a larger share; DEBUG (needs conversational
# continuity — "what did we just try?") gets less. Unlisted intents keep
# ContextBudget's original fixed 0.55 default.
_RETRIEVAL_BUDGET_BIAS: dict[IntentType, float] = {
    IntentType.SEARCH: 0.7,
    IntentType.DEPENDENCY_ANALYSIS: 0.7,
    IntentType.ARCHITECTURE: 0.65,
    IntentType.REFACTOR: 0.6,
    IntentType.REVIEW: 0.6,
    IntentType.DEBUG: 0.45,
}


async def build_turn_context(
    repl: VeluneREPL, text: str, model: ModelDescriptor
) -> tuple[list[dict], ContextAssemblyReport, IntentType, float]:
    """Assemble the message list for one chat turn via ``ContextAssembler``.

    ``text`` is the already mention-resolved, hook-transformed user prompt,
    and ``repl._conversation`` already has that turn appended (see
    ``_handle_prompt``). Returns ``(messages, report, intent, confidence)``.
    """
    intent, confidence = IntentClassifier().classify_with_confidence(text)
    mode_config = repl._mode_manager.config
    workspace = Path(repl.container.get("runtime.workspace") or ".")

    await _await_memory_subsystems(repl)

    retrieval_bias = _RETRIEVAL_BUDGET_BIAS.get(intent, 0.55)
    budget = ContextBudget.for_chat(
        repl._mode_manager.current, model.context_length, retrieval_bias=retrieval_bias
    )
    repo_snapshot_budget = (
        _REPO_SNAPSHOT_BIASED_TOKENS
        if intent in _REPO_HEAVY_INTENTS
        else _REPO_SNAPSHOT_BASE_TOKENS
    )

    base_depth = max(1, mode_config.retrieval_depth)
    depth = base_depth if intent in _DEEP_RETRIEVAL_INTENTS else max(1, base_depth // 2)

    chunks: list[ContextChunk] = []

    # ── SYSTEM_PROMPT (always present, never trimmed) ────────────────────
    system_text = (
        f"Velune session — mode: {repl._mode_manager.current.value}, model: {model.model_id}."
    )
    chunks.append(
        ContextChunk(
            section=ContextSection.SYSTEM_PROMPT,
            content=system_text,
            token_count=estimate_tokens(system_text),
            source="session",
            trust_score=1.0,
            priority=1.0,
        )
    )

    # The four retrieval sources below are independent (none reads another's
    # output and none mutates `repl`), so they run concurrently instead of
    # serially: per-turn retrieval overhead becomes the *slowest* source rather
    # than the *sum* of all four. Each coroutine already guards itself and
    # returns empty on any failure, so gather never raises here. Results are
    # spliced back in the original section order to keep chunk ordering stable.
    #   - RETRIEVED_CONTEXT: hybrid file/code retrieval, planned by intent
    #   - RETRIEVED_CONTEXT: three-brain fan-out (semantic/episodic/kg)
    #   - COGNITIVE_CONTINUITY: lineage decisions/failures
    #   - REPOSITORY_SNAPSHOT / ARCHITECTURAL_DRIFT
    hybrid_chunks, memory_chunks, continuity_chunk, repo_chunks = await asyncio.gather(
        _retrieve_hybrid(repl, text, depth, intent, confidence),
        _retrieve_via_memory_lifecycle(repl, text, workspace, depth, budget.retrieval_allocation),
        _lineage_chunk(repl, text),
        _repository_snapshot_chunks(repl, repo_snapshot_budget),
    )
    chunks.extend(hybrid_chunks)
    chunks.extend(memory_chunks)
    if continuity_chunk is not None:
        chunks.append(continuity_chunk)
    chunks.extend(repo_chunks)

    # ── WORKING_MEMORY: prior conversation turns ─────────────────────────
    conversation = repl._conversation
    if mode_config.context_compression and conversation:
        from velune.context.extractive import compress_conversation

        conversation = compress_conversation(
            conversation, max_tokens=mode_config.max_context_tokens
        )
        repl._conversation = conversation

    # The just-appended current turn becomes its own CURRENT_PROMPT chunk
    # below — excluding it here avoids sending the same text twice.
    history = (
        conversation[:-1]
        if conversation and conversation[-1].get("role") == "user"
        else conversation
    )
    for msg in history:
        content = msg.get("content", "")
        if not content:
            continue
        chunks.append(
            ContextChunk(
                section=ContextSection.WORKING_MEMORY,
                content=content,
                token_count=estimate_tokens(content),
                source=f"conversation:{msg.get('role', 'unknown')}",
                trust_score=1.0,
                priority=0.5,
                metadata={"role": msg.get("role", "unknown")},
            )
        )

    # ── CURRENT_PROMPT (always present, never trimmed, always last) ──────
    chunks.append(
        ContextChunk(
            section=ContextSection.CURRENT_PROMPT,
            content=text,
            token_count=estimate_tokens(text),
            source="user",
            trust_score=1.0,
            priority=1.0,
        )
    )

    assembled_context, report = ContextAssembler().assemble(chunks, budget, model)
    _record_retrieval_feedback(repl, text, intent, confidence, chunks, report)

    messages: list[dict] = []
    if assembled_context:
        messages.append({"role": "system", "content": assembled_context})
    messages.append({"role": "user", "content": text})

    return messages, report, intent, confidence


async def _await_memory_subsystems(repl: VeluneREPL) -> None:
    """Give Tier-1 memory/retrieval a brief chance to finish warming.

    Retrieval, three-brain, and repository cognition are warmed in the
    background so the prompt is interactive immediately. Every helper below
    degrades to an empty list when its subsystem is missing, and does so at
    ``debug`` level — so a prompt typed during the warm-up window silently got
    no memory, no retrieval, and no repository context, indistinguishable from
    "nothing relevant was found".

    ``ServiceContainer.wait_ready`` was built for exactly this and had no
    callers. The wait is short and only ever pays out on the first turn or two
    of a cold start; after that every key is already registered and each call
    returns immediately.
    """
    container = repl.container
    waiter = getattr(container, "wait_ready", None)
    if waiter is None:
        return

    pending = [key for key in _WARM_GATED_KEYS if not container.is_ready(key)]
    if not pending:
        return

    started = time.monotonic()
    results = await asyncio.gather(
        *(waiter(key, timeout=_WARMUP_WAIT_S) for key in pending),
        return_exceptions=True,
    )
    waited = time.monotonic() - started

    missing = [key for key, ok in zip(pending, results, strict=False) if ok is not True]
    if missing:
        _log.info(
            "Proceeding with reduced context after %.1fs: %s still warming.",
            waited,
            ", ".join(sorted(missing)),
        )
    else:
        _log.debug("Tier-1 memory subsystems became ready after %.2fs.", waited)


def _record_retrieval_feedback(
    repl: VeluneREPL,
    text: str,
    intent: IntentType,
    confidence: float,
    chunks: list[ContextChunk],
    report: ContextAssemblyReport,
) -> None:
    """Best-effort: record this turn's retrieval outcome. Never blocks or fails the turn."""
    try:
        recorder = (
            repl.container.get("runtime.retrieval_feedback")
            if repl.container.has("runtime.retrieval_feedback")
            else None
        )
        if recorder is None:
            return
        from velune.retrieval.feedback import hit_counts_by_source

        retrieved_sources = [
            c.source for c in chunks if c.section == ContextSection.RETRIEVED_CONTEXT
        ]
        recorder.record(
            query_text=text,
            intent=intent.value,
            confidence=confidence,
            hit_counts_by_source=hit_counts_by_source(retrieved_sources),
            report=report,
        )
    except Exception as exc:
        _log.debug("Retrieval feedback recording failed (non-fatal): %s", exc)


async def _retrieve_hybrid(
    repl: VeluneREPL, text: str, top_k: int, intent: IntentType, confidence: float
) -> list[ContextChunk]:
    """Query HybridRetriever, planning the fusion strategy from *intent* rather
    than always using the same fixed weights regardless of what's being asked."""
    try:
        retrieval = repl.container.get("runtime.retrieval")
    except Exception:
        return []
    if not retrieval:
        return []
    try:
        planner = (
            repl.container.get("runtime.retrieval_planner")
            if repl.container.has("runtime.retrieval_planner")
            else None
        )
        if planner is None:
            from velune.retrieval.planner import RetrievalPlanner

            planner = RetrievalPlanner()

        result = await asyncio.wait_for(
            planner.plan_and_retrieve(retrieval, intent, confidence, text, namespace=None),
            timeout=_RETRIEVAL_TIMEOUT_S,
        )
        # top_k from the planner's intent-tuned strategy takes priority; still
        # respect the caller's depth as an upper bound so mode_config.retrieval_depth
        # (OPTIMUS/NORMAL/GODLY) continues to cap retrieval breadth as before.
        if result.hits and top_k:
            result.hits = result.hits[:top_k]
    except Exception as exc:
        _log.debug("Hybrid retrieval failed (non-fatal): %s", exc)
        return []
    if not result or not result.hits:
        return []

    chunks: list[ContextChunk] = []
    for hit in result.hits:
        content = hit.document.content
        if not content:
            continue
        chunks.append(
            ContextChunk(
                section=ContextSection.RETRIEVED_CONTEXT,
                content=content,
                token_count=estimate_tokens(content),
                source=f"hybrid_retrieval:{hit.source.value}",
                trust_score=max(0.0, min(1.0, hit.score)),
                priority=0.5,
            )
        )
    return chunks


_MEMORY_SOURCE_LABELS = {
    "semantic": "semantic_memory",
    "episodic": "episodic_memory",
    "kg": "knowledge_graph",
}
_MEMORY_SOURCE_PRIORITY = {
    "semantic": 0.5,
    "episodic": 0.4,
    "kg": 0.6,
}


async def _retrieve_via_memory_lifecycle(
    repl: VeluneREPL, text: str, workspace: Path, depth: int, budget_tokens: int
) -> list[ContextChunk]:
    """Vitality-aware semantic/episodic/kg retrieval via ``MemoryLifecycleManager``.

    Previously this queried ``ThreeBrainCoordinator`` directly and reimplemented
    a cruder version of trust shaping inline (flat 0.6-0.7 trust scores, no
    age-based decay). ``MemoryLifecycleManager.retrieve()`` — built for exactly
    this and already used elsewhere in this file for lineage warnings — wraps
    the same coordinator (they're the same injected instance; see
    ``velune/memory/subsystems.py::_create_memory_lifecycle``) and adds the
    LIVE/ZOMBIE/ARCHIVED vitality classification and trust decay it was
    designed for, so a stale semantic hit from a month ago no longer carries
    the same weight as one from the current session.

    ``working`` results are dropped: ``repl._conversation`` already supplies
    WORKING_MEMORY directly, and once ``record_turn()`` fills
    ``runtime.working_memory`` too, including its hits would double-count the
    same turns.
    """
    try:
        manager = repl.container.get("runtime.memory_lifecycle")
    except Exception:
        return []
    if not manager:
        return []

    try:
        retrieved = await manager.retrieve(
            text, workspace_root=str(workspace), budget=budget_tokens, depth=depth
        )
    except Exception as exc:
        _log.debug("MemoryLifecycleManager retrieval failed (non-fatal): %s", exc)
        return []

    chunks: list[ContextChunk] = []
    for result in retrieved.results:
        if result.source_type == "working" or not result.content:
            continue
        chunks.append(
            ContextChunk(
                section=ContextSection.RETRIEVED_CONTEXT,
                content=result.content,
                token_count=estimate_tokens(result.content),
                source=_MEMORY_SOURCE_LABELS.get(result.source_type, result.source_type),
                trust_score=max(0.0, min(1.0, result.trust_score)),
                priority=_MEMORY_SOURCE_PRIORITY.get(result.source_type, 0.5),
                metadata={"vitality": result.vitality, "attribution": result.attribution},
            )
        )
    return chunks


async def _lineage_chunk(repl: VeluneREPL, text: str) -> ContextChunk | None:
    """Surface architectural decisions/failures relevant to this turn."""
    try:
        manager = repl.container.get("runtime.memory_lifecycle")
    except Exception:
        return None
    if not manager:
        return None
    try:
        decisions, failures = await manager.get_lineage_warnings(text)
    except Exception as exc:
        _log.debug("Lineage warning lookup failed (non-fatal): %s", exc)
        return None
    if not decisions and not failures:
        return None

    lines: list[str] = []
    for d in decisions[:5]:
        lines.append(f"  decision [{d.target_subsystem}]: {d.rationale}")
    for f in failures[:5]:
        lines.append(f"  failure [{f.target_subsystem}] {f.error_type}: {f.error_message}")
    content = "[COGNITIVE CONTINUITY]\n" + "\n".join(lines)
    return ContextChunk(
        section=ContextSection.COGNITIVE_CONTINUITY,
        content=content,
        token_count=estimate_tokens(content),
        source="lineage_memory",
        trust_score=0.8,
        priority=0.7,
    )


async def _repository_snapshot_chunks(
    repl: VeluneREPL, max_snapshot_tokens: int
) -> list[ContextChunk]:
    """Cheap Repository Brain participation — reads the incremental pipeline cache.

    Uses ``RepositoryCognitionService.get_snapshot_fresh()``, which merges the
    dependency edges + API connection map that the background
    ``RepositoryIntelligenceEngine`` maintains incrementally onto the file/symbol
    snapshot. The plain ``get_snapshot()`` returns ``edges=[]`` and no
    ``api_map``, so those context sections rendered empty on every chat turn —
    the incremental cognition was computed but never surfaced. This does no
    per-turn reindexing: it's a cache read + merge, with a one-time full
    ``index()`` only on true cold start (then seeded for the fast path).
    """
    try:
        repo_service = repl.container.get("runtime.repository_cognition")
    except Exception:
        return []
    if not repo_service:
        return []

    try:
        fresh = getattr(repo_service, "get_snapshot_fresh", None)
        if fresh is not None:
            # Bounded like its sibling _retrieve_hybrid. On a cold cache this
            # call falls through to a full repository index, which previously
            # ran unbounded inside the turn's gather — the first prompt in an
            # unindexed repo blocked until the whole tree had been walked. The
            # background auto-index warms the cache regardless, so a timeout
            # here costs this turn some context, not the feature.
            snapshot = await asyncio.wait_for(fresh(), timeout=_REPO_SNAPSHOT_TIMEOUT_S)
        else:  # defensive: mocks/older services without the fast path
            snapshot = repo_service.get_snapshot()
    except asyncio.TimeoutError:
        _log.debug(
            "Repository snapshot exceeded %.1fs (likely a cold index); "
            "continuing without repository context this turn.",
            _REPO_SNAPSHOT_TIMEOUT_S,
        )
        return []
    except Exception as exc:
        _log.debug("Repository snapshot read failed (non-fatal): %s", exc)
        return []
    if snapshot is None:
        return []

    try:
        from velune.repository.context_builder import WorkspaceContextBuilder

        builder = WorkspaceContextBuilder()
        snapshot_text, drift_text = builder.build(
            snapshot,
            delta=None,
            max_snapshot_tokens=max_snapshot_tokens,
            api_map=getattr(snapshot, "api_map", None),
        )
    except Exception as exc:
        _log.debug("Repository snapshot build failed (non-fatal): %s", exc)
        return []

    chunks: list[ContextChunk] = []
    if snapshot_text:
        wrapped = _wrap_workspace_content(repl, "repository_context", snapshot_text)
        chunks.append(
            ContextChunk(
                section=ContextSection.REPOSITORY_SNAPSHOT,
                content=wrapped,
                token_count=estimate_tokens(wrapped),
                source="repository_cognition",
                trust_score=0.9,
                priority=0.6,
            )
        )
    if drift_text:
        wrapped_drift = _wrap_workspace_content(repl, "architectural_drift", drift_text)
        chunks.append(
            ContextChunk(
                section=ContextSection.ARCHITECTURAL_DRIFT,
                content=wrapped_drift,
                token_count=estimate_tokens(wrapped_drift),
                source="repository_cognition",
                trust_score=1.0,
                priority=1.0,
            )
        )
    return chunks


def _wrap_workspace_content(repl: VeluneREPL, name: str, content: str) -> str:
    """Route workspace-derived text through the same untrusted-content boundary
    ``CouncilOrchestrator`` already uses, so repo-derived text is never treated
    as instructions by the model.
    """
    try:
        firewall = repl.container.get("runtime.firewall")
    except Exception:
        firewall = None
    if firewall is None:
        from velune.cognition.firewall import CognitiveFirewall

        firewall = CognitiveFirewall()
    try:
        scan = firewall.scan_file_for_injection(name, content)
        if scan.get("quarantined"):
            content = scan.get("neutralized_content", "")
        return firewall.wrap_workspace_content(name, content)
    except Exception:
        return content
