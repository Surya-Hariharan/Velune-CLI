"""LangGraph-style orchestrator compiling council roles and executing deliberation flows."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune.memory.tiers.lineage import LineageMemoryTier

if TYPE_CHECKING:
    from velune.kernel.config import VeluneConfig
    from velune.memory.storage.sqlite_manager import SQLiteManager
    from velune.memory.storage.sqlite_pool import SQLiteConnectionPool

from velune.cognition.arbitrator import CouncilArbitrator
from velune.cognition.architecture import ArchitectureCognitionAgent
from velune.cognition.budget import CouncilExecutionBudget
from velune.cognition.council.debate import calculate_max_debate_turns
from velune.cognition.council.factory import CouncilAgentFactory
from velune.cognition.council.tiers import CouncilTier, TierClassifier
from velune.cognition.style_resolver import StyleResolver
from velune.core.trace import TracedLogger
from velune.models.specializations import CouncilRole, ModelSpecializationMapper
from velune.orchestration.schemas import StreamProgress
from velune.providers.registry import ProviderRegistry
from velune.telemetry.cognition import CognitivePerformanceAnalytics

logger = TracedLogger("velune.cognition.orchestrator")


class CouncilOrchestrator:
    """Manages model mappings and runs the multi-agent Reasoning Council debate graph."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        mapper: ModelSpecializationMapper,
        historical_accuracy: float = 0.85,
        lineage_db_path: Path | None = None,
        analytics: CognitivePerformanceAnalytics | None = None,
        sqlite_manager: SQLiteManager | None = None,
        config: VeluneConfig | None = None,
        lineage_memory: LineageMemoryTier | None = None,
        pool: SQLiteConnectionPool | None = None,
    ) -> None:
        self.provider_registry = provider_registry
        self.mapper = mapper
        self.arbitrator = CouncilArbitrator(historical_accuracy=historical_accuracy)
        self.architecture_agent = ArchitectureCognitionAgent(workspace_root=None, ledger=None)
        self.config = config

        # In production the pre-created lineage_memory is always injected;
        # the fallback is only used when constructing standalone (tests / CLI).
        if lineage_memory is not None:
            self.lineage_memory: LineageMemoryTier | None = lineage_memory
        elif pool is not None:
            from velune.memory.tiers.lineage import LineageMemoryTier as _LineageMemoryTier

            self.lineage_memory = _LineageMemoryTier(pool)
        else:
            self.lineage_memory = None
        self.analytics = analytics or CognitivePerformanceAnalytics(sqlite_manager=sqlite_manager)

        from velune.cognition.firewall import CognitiveFirewall

        self.firewall = CognitiveFirewall()

        self.max_wall_time_seconds = float(
            os.environ.get("VELUNE_COUNCIL_MAX_SECONDS", "600")  # 10 minutes default
        )
        self._states: dict[str, Any] = {}
        self._live_lock = asyncio.Lock()

        # Owns all concurrency decisions for council rounds (honest sequential on
        # a shared backend, concurrent across distinct providers).
        from velune.cognition.council.scheduler import CouncilScheduler

        self.scheduler = CouncilScheduler()

        # Extracted Subsystems
        self.agent_factory = CouncilAgentFactory(
            provider_registry=self.provider_registry, mapper=self.mapper, live_lock=self._live_lock
        )
        self.style_resolver = StyleResolver(lineage_memory=self.lineage_memory)

        # Reduce Service Locator usage: resolve task_registry once in constructor if not passed
        task_registry = None
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if container.has("runtime.task_registry"):
                task_registry = container.get("runtime.task_registry")
        except Exception:
            pass

        max_tier = "full"
        default_override = "auto"
        if config and hasattr(config, "cognition"):
            max_tier = config.cognition.max_council_tier
            default_override = config.cognition.default_tier_override

        low_resource_mode = (config and config.execution.low_resource_mode) or os.environ.get(
            "VELUNE_LOW_RESOURCE", ""
        ).lower() in ("true", "1", "yes")

        self.tier_classifier = TierClassifier(
            task_registry=task_registry,
            max_council_tier=max_tier,
            default_tier_override=default_override,
            low_resource_mode=low_resource_mode,
        )

        # Error loop detection + retry policy — wired into agent call sites
        from velune.core.loop_detector import ErrorLoopDetector
        from velune.core.retry import RetryPolicy

        self._loop_detector = ErrorLoopDetector()
        _max_retries = 3
        if config and hasattr(config, "providers"):
            _max_retries = getattr(config.providers, "max_retries", 3)
        self._retry_policy = RetryPolicy(
            max_attempts=_max_retries,
            base_delay_s=2.0,
            max_delay_s=30.0,
            jitter=True,
            loop_detector=self._loop_detector,
        )

        # Resolve the event bus for retry event emission (best-effort)
        self._bus: Any | None = None
        try:
            from velune.kernel.registry import get_container

            _c = get_container()
            if _c.has("runtime.bus"):
                self._bus = _c.get("runtime.bus")
        except Exception:
            pass

    def get_state(self, run_id: str) -> Any | None:
        """Get the cached OrchestrationState by run_id."""
        return self._states.get(run_id)

    async def _diverge_candidates(
        self,
        coder: Any,
        *,
        prompt: str,
        current_code: str,
        plan_context: str,
        style_profile: Any,
        format_instructions: str,
        n_samples: int,
        timeout: float,
        progress_callback: Any | None = None,
    ) -> list[str]:
        """Round 1 (Diverge): draw *n_samples* independent Coder candidates.

        Samples use staggered temperatures from the Coder's sampling profile so
        the solutions genuinely diverge (self-consistency). All samples share the
        Coder's single backend, so the scheduler runs them sequentially and says
        so — no fake parallelism. Failed/timed-out samples are dropped; the
        surviving candidates feed the self-consistency vote.
        """
        from velune.cognition.council.sampling import get_sampling_profile
        from velune.cognition.council.scheduler import CouncilJob
        from velune.models.specializations import CouncilRole

        n_samples = max(1, n_samples)
        temps = get_sampling_profile(CouncilRole.CODER).sample_temperatures(n_samples)
        provider_id = coder.model.provider_id

        def _make_job(idx: int, temp: float) -> CouncilJob:
            async def _run() -> str:
                return await coder.write_code(
                    prompt=prompt,
                    current_code=current_code,
                    plan_context=plan_context,
                    style_profile=style_profile,
                    format_instructions=format_instructions,
                    temperature=temp,
                )

            return CouncilJob(name=f"coder#{idx}", provider_id=provider_id, run=_run)

        jobs = [_make_job(i, t) for i, t in enumerate(temps)]
        if progress_callback and n_samples > 1:
            progress_callback(f"[Coder] Diverge round: {n_samples} candidate solutions...")

        results = await self.scheduler.run(jobs, timeout=timeout)
        candidates = [r.value for r in results if r.ok and isinstance(r.value, str) and r.value]
        return candidates

    async def stream(self, prompt: str) -> AsyncIterator[StreamProgress]:
        """Runs the Reasoning Council task execution and streams milestones."""
        import uuid

        from velune.orchestration.schemas import (
            ExecutionStatus,
            OrchestrationRequest,
            OrchestrationState,
            StreamProgress,
        )

        run_id = f"run-{uuid.uuid4().hex[:12]}"
        queue = asyncio.Queue()

        # Persist the real execution stream so `velune trace` can replay it. This
        # is the single chokepoint every milestone (for every consumer — REPL,
        # `ask`, `run`) passes through, so tracing here captures the genuine
        # planner→coder→reviewer→synthesis flow with no fabrication. Best-effort:
        # a missing workspace or write error must never break a run.
        trace_log = None
        try:
            from velune.kernel.registry import get_container

            _container = get_container()
            if _container.has("runtime.workspace"):
                from velune.observability.trace_sink import trace_log_for_workspace

                trace_log = trace_log_for_workspace(_container.get("runtime.workspace"))
        except Exception:
            trace_log = None

        _seq = 0
        _last_phase: list[str] = [""]
        _phase_start: list[float] = [time.monotonic()]

        def progress_callback(msg: str):
            nonlocal _seq
            phase = ""
            message = msg
            if msg.startswith("[") and "]" in msg:
                parts = msg.split("]", 1)
                phase = parts[0][1:].lower()
                message = parts[1].strip()

            elapsed: float | None = None
            if phase and phase != _last_phase[0]:
                now = time.monotonic()
                if _last_phase[0]:
                    elapsed = (now - _phase_start[0]) * 1000
                _last_phase[0] = phase
                _phase_start[0] = now

            queue.put_nowait(
                StreamProgress(run_id=run_id, phase=phase, message=message, elapsed_ms=elapsed)
            )
            if trace_log is not None:
                from velune.observability.trace_sink import record_milestone

                _seq += 1
                record_milestone(trace_log, run_id, _seq, phase, message)

        # First, emit context reconstruction milestone before indexing
        progress_callback("[Context Reconstruction] Gathering repository context snapshot...")

        repo_context = "Repository context unavailable."
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if container.has("runtime.repository_cognition"):
                repository_cognition = container.get("runtime.repository_cognition")

                # Before indexing, probe for file-system changes so that files the
                # user created/deleted since the last prompt are already reflected in
                # the snapshot.  The git-SHA fast path makes this effectively free
                # when nothing has changed.
                await repository_cognition.probe_for_changes()

                snapshot = repository_cognition.index(force=False)
                if snapshot:
                    from velune.repository.context_builder import WorkspaceContextBuilder

                    builder = WorkspaceContextBuilder()
                    # Pass the live api_map object if it was attached by the pipeline.
                    api_map = getattr(snapshot, "api_map", None)
                    snapshot_text, drift_text = builder.build(
                        snapshot,
                        delta=repository_cognition.last_delta,
                        api_map=api_map,
                    )
                    # Clear the consumed delta so the next run doesn't re-announce it.
                    repository_cognition._last_delta = None

                    # Firewall: scan for prompt injection in workspace-derived content.
                    scan_res = self.firewall.scan_file_for_injection("repo_context", snapshot_text)
                    if scan_res.get("quarantined"):
                        snapshot_text = scan_res.get("neutralized_content", "")
                        logger.warning(
                            "Repository context neutralized by firewall before prompt injection."
                        )

                    # Always wrap in untrusted-content boundary so agents treat
                    # file-system data as data, not instructions.
                    repo_context = self.firewall.wrap_workspace_content(
                        "repository_context", snapshot_text
                    )

                    # Surface architectural violations in the ARCHITECTURAL_DRIFT
                    # progress milestone so they appear in the trace.
                    if drift_text:
                        progress_callback(f"[Context Reconstruction] {drift_text}")
        except Exception as e:
            logger.warning("Could not gather repository snapshot: %s", e)

        # Inject git state as a separate firewall-bounded context block so the
        # council agents know the current branch, staged diff, and recent commits.
        try:
            from velune.kernel.registry import get_container as _get_container
            from velune.repository.git_context import GitContextProvider

            _c = _get_container()
            _ws = _c.get("runtime.workspace") if _c.has("runtime.workspace") else "."
            _git_provider = GitContextProvider(Path(_ws))
            _git_snap = await asyncio.to_thread(_git_provider.gather)
            _git_block = _git_provider.build_context_block(_git_snap)
            if _git_block:
                repo_context += "\n\n" + self.firewall.wrap_workspace_content(
                    "git_context", _git_block
                )
        except Exception as _ge:
            logger.debug("Git context gathering failed (non-fatal): %s", _ge)

        # Inject config file intelligence so the council knows project commands and deps.
        try:
            from velune.kernel.registry import get_container as _get_container2
            from velune.repository.config_intelligence import ConfigIntelligenceExtractor
            from velune.repository.project_type import ProjectTypeDetector

            _c2 = _get_container2()
            _ws2 = _c2.get("runtime.workspace") if _c2.has("runtime.workspace") else "."
            _profile = ProjectTypeDetector().detect(Path(_ws2))
            _intel = ConfigIntelligenceExtractor().extract(Path(_ws2), _profile.config_files)
            _config_block = ConfigIntelligenceExtractor().render_context_block(_intel)
            if _config_block:
                repo_context += "\n\n" + self.firewall.wrap_workspace_content(
                    "project_config", _config_block
                )
        except Exception as _ce:
            logger.debug("Config intelligence extraction failed (non-fatal): %s", _ce)

        async def run_execution():
            coder_proposal: str | None = None
            try:
                from velune.core.retry import retry_async

                result = await retry_async(
                    self._retry_policy,
                    lambda: self.execute_task(
                        prompt=prompt,
                        repo_context=repo_context,
                        progress_callback=progress_callback,
                    ),
                    bus=self._bus,
                    source="council_orchestrator",
                )
                final_summary = result.get("final_summary", "Execution completed successfully.")
                status = ExecutionStatus.COMPLETED
                error = None
                task_plan = result.get("task_plan")
                coder_proposal = result.get("coder_proposal")
            except asyncio.CancelledError:
                logger.info("Reasoning Council execution was cancelled.")
                status = ExecutionStatus.FAILED
                error = "Cancelled"
                final_summary = "Execution cancelled by user."
                task_plan = None

                request = OrchestrationRequest(prompt=prompt, workspace=".")
                state = OrchestrationState(
                    run_id=run_id,
                    request=request,
                    status=status,
                    output=final_summary,
                    error=error,
                    task_plan=task_plan,
                )
                self._states[run_id] = state
                queue.put_nowait(None)
                raise
            except Exception as e:
                logger.error("Reasoning Council execution failed: %s", e)
                final_summary = f"Execution failed: {e}"
                status = ExecutionStatus.FAILED
                error = str(e)
                task_plan = None

            request = OrchestrationRequest(prompt=prompt, workspace=".")
            state = OrchestrationState(
                run_id=run_id,
                request=request,
                status=status,
                output=final_summary,
                coder_proposal=coder_proposal,
                error=error,
                task_plan=task_plan,
            )
            self._states[run_id] = state
            queue.put_nowait(None)  # Sentinel

        execution_task = asyncio.create_task(run_execution())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not execution_task.done():
                execution_task.cancel()
                try:
                    await execution_task
                except asyncio.CancelledError:
                    pass

    async def _get_or_refresh_style_profile(self, target_file: str) -> dict[str, Any] | None:
        """Queries the style profile from database, or scans and caches it if missing/stale."""
        return await self.style_resolver.get_or_refresh_style_profile(target_file)

    def _is_structural_change(self, prompt: str, repo_context: str) -> bool:
        """
        Determines if a task requires structural modifications or is simple/trivial.
        Simple changes: UI tweaks, comment changes, formatting, small edits, basic lookups.
        Structural changes: class/method definitions, interface changes, multiple files, DB/concurrency related.
        """
        prompt_lower = prompt.lower()

        # 1. Integrate target-file structural awareness and fan-in signal
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if container.has("runtime.repository_cognition"):
                repo_service = container.get("runtime.repository_cognition")
                grapher = repo_service.grapher

                # Scan prompt for mentioned files
                mentioned_files = re.findall(r"[\w\/\.\-]+\.(?:py|js|ts|go|rs)", prompt)
                for mf in mentioned_files:
                    # 1a. Core path structural awareness
                    norm_path = mf.replace("\\", "/").lower()
                    if "core/" in norm_path or "kernel/" in norm_path or "schemas/" in norm_path:
                        return True

                    # 1b. Direct fan-in signal
                    dependents = grapher.get_dependents(mf)
                    if len(dependents) >= 3:
                        return True
        except Exception:
            pass

        # 2. Structural indicators
        structural_keywords = [
            "redesign",
            "architect",
            "concurrency",
            "thread",
            "async",
            "lock",
            "database",
            "class",
            "interface",
            "refactor",
            "performance",
            "scalability",
            "security",
            "cohesion",
            "module",
            "coupling",
            "sandbox",
            "boundary",
            "lcom",
            "critic",
        ]

        for kw in structural_keywords:
            if kw in prompt_lower:
                return True

        # 3. Simple indicators
        simple_keywords = [
            "typo",
            "comment",
            "format",
            "rename variable",
            "ui text",
            "alignment",
            "simple tweak",
        ]
        for kw in simple_keywords:
            if kw in prompt_lower:
                return False

        # 4. Length heuristic
        if len(prompt.split()) > 15:
            return True

        return False

    def estimate_blast_radius(self, target_file: str) -> float:
        """
        Estimates structural impact using direct dependents (depth 1)
        and transitive dependents (depth 2) with depth attenuation.
        Caps search and normalizes dynamically into standard range [0.1, 0.9].
        """
        import math

        # Defaults
        default_score = 0.3
        if any(kw in target_file.lower() for kw in ["core", "kernel", "engine", "base", "schemas"]):
            default_score = 0.7

        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if not container.has("runtime.repository_cognition"):
                return default_score

            repo_service = container.get("runtime.repository_cognition")
            grapher = repo_service.grapher
            rel_file = grapher._to_rel_path(target_file)

            if rel_file not in grapher.graph:
                return default_score

            # Depth 1 dependents
            deps_d1 = set(grapher.get_dependents(rel_file))

            # Depth 2 dependents
            deps_d2 = set()
            for d1 in deps_d1:
                for d2 in grapher.get_dependents(d1):
                    if d2 != rel_file and d2 not in deps_d1:
                        deps_d2.add(d2)

            # Cap traversals at a safe boundary to preserve latency
            d1_list = list(deps_d1)[:30]
            d2_list = list(deps_d2)[:20]

            raw_score = 1.0 * len(d1_list) + 0.5 * len(d2_list)

            # Normalize dynamically to [0.1, 0.9] range
            normalized_score = 0.1 + 0.8 * (1.0 - math.exp(-raw_score / 5.0))
            return round(normalized_score, 3)
        except Exception:
            return default_score

    def _resolve_tier(
        self,
        prompt: str,
        repo_context: str,
        council_tier: str | None = None,
    ) -> CouncilTier:
        """Resolve council tier once, considering configuration and resources."""
        roles = self.mapper.map_roles()
        coder_model = roles.get(CouncilRole.CODER)
        estimated_tps = 8.0  # conservative default
        if coder_model:
            profile = self.mapper.profiler.get_profile(
                coder_model.provider_id, coder_model.model_id
            )
            if profile and profile.tps > 0.0:
                estimated_tps = profile.tps

        # Temporarily apply override if specified
        original_override = self.tier_classifier.default_tier_override
        if council_tier:
            self.tier_classifier.default_tier_override = council_tier

        try:
            return self.tier_classifier.classify(prompt, repo_context, estimated_tps)
        finally:
            self.tier_classifier.default_tier_override = original_override

    async def execute_task(
        self,
        prompt: str,
        repo_context: str,
        council_tier: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
        budget: CouncilExecutionBudget | None = None,
    ) -> dict[str, Any]:
        """Orchestrate a complete council deliberation pass for a task prompt with wall-time limit."""
        await self._maybe_gate_cost(prompt, repo_context)
        budget = budget or CouncilExecutionBudget(
            max_wall_time_seconds=int(self.max_wall_time_seconds)
        )
        tier = self._resolve_tier(prompt, repo_context, council_tier)
        tier_str = tier.value

        try:
            return await asyncio.wait_for(
                self._execute_tiered(
                    prompt=prompt,
                    repo_context=repo_context,
                    tier=tier,
                    progress_callback=progress_callback,
                    budget=budget,
                ),
                timeout=budget.max_wall_time_seconds,
            )
        except TimeoutError:
            logger.error(
                "Council wall-time limit reached (%ds). Returning partial result.",
                budget.max_wall_time_seconds,
            )
            return self._build_timeout_result(prompt, tier_str)

    async def _maybe_gate_cost(self, prompt: str, repo_context: str) -> None:
        """Prompt for confirmation when estimated cloud cost exceeds the configured threshold.

        Skipped when VELUNE_YES=1 is set or the yes flag has been propagated via CLIContext.
        Only fires for tasks estimated > 2 000 tokens.
        """
        import sys

        if os.environ.get("VELUNE_YES", "").lower() in ("1", "true", "yes"):
            return

        try:
            from velune.telemetry.cost_estimator import CostEstimator

            # Resolve the registry to find which cloud model will be used
            try:
                from velune.kernel.registry import get_container

                container = get_container()
                if container.has("runtime.provider_registry"):
                    container.get("runtime.provider_registry")
            except Exception:
                pass

            models: list = []
            try:
                if container.has("runtime.model_registry"):
                    model_reg = container.get("runtime.model_registry")
                    models = model_reg.list_all()
            except Exception:
                pass

            cloud_model = next(
                (m for m in models if not getattr(m, "is_local", False)),
                None,
            )
            if cloud_model is None:
                return

            messages = [
                {"role": "system", "content": repo_context},
                {"role": "user", "content": prompt},
            ]
            estimator = CostEstimator()
            token_count = estimator.estimate_tokens(messages, cloud_model)

            if token_count <= 2000:
                return

            cost = estimator.estimate_cost(token_count, cloud_model)
            if cost is None:
                return

            threshold = 0.01
            if self.config and hasattr(self.config, "providers"):
                threshold = getattr(self.config.providers, "cost_threshold_usd", 0.01)

            if cost <= threshold:
                return

            estimate_str = estimator.format_estimate(token_count, cost, cloud_model)
            sys.stdout.write(f"\nEstimated cost: {estimate_str}. Proceed? [Y/n] ")
            sys.stdout.flush()
            answer = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
            if answer.strip().lower() in ("n", "no"):
                raise asyncio.CancelledError("Aborted by user due to cost estimate.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Cost gate skipped due to error: %s", exc)

    def _build_timeout_result(self, prompt: str, tier: str = "full") -> dict[str, Any]:
        from velune.cognition.council.messages import ReviewerMessage

        return {
            "tier": tier,
            "task_plan": None,
            "coder_proposal": None,
            "reviewer_report": ReviewerMessage(
                passed=False,
                critical_issues=["Council execution timed out."],
                confidence_rating=0.0,
            ),
            "challenger_report": None,
            "scalability_report": None,
            "security_report": None,
            "performance_report": None,
            "maintainability_report": None,
            "arbitration": {
                "overall_confidence": 0.0,
                "requires_human_review": True,
                "flags": ["TIMEOUT"],
                "winning_claims": [],
                "synthesis_instructions": "",
            },
            "final_summary": "Council execution timed out. Partial analysis: (None)",
            "is_timeout": True,
        }

    @staticmethod
    def _extract_target_file(prompt: str) -> str:
        target_file = "velune/core/main.py"
        py_files = re.findall(r"[\w\/\.\-]+\.py", prompt)
        if py_files:
            target_file = py_files[0]
        return target_file

    async def _execute_tiered(
        self,
        prompt: str,
        repo_context: str,
        tier: CouncilTier,
        progress_callback: Callable[[str], None] | None = None,
        run_id: str = "default",
        budget: CouncilExecutionBudget | None = None,
    ) -> dict[str, Any]:
        """Consolidated orchestrator execution path for all tiers."""
        import uuid

        from velune.cognition.council.messages import (
            ChallengerMessage,
            CriticMessage,
            ReviewerMessage,
        )
        from velune.cognition.firewall import CognitiveFirewall
        from velune.core.trace import TraceContext

        if run_id == "default":
            run_id = f"council-{uuid.uuid4().hex[:8]}"

        tier_level = {"instant": 1, "minimal": 2, "standard": 3, "full": 4}[tier.value]
        budget = budget or CouncilExecutionBudget()

        with TraceContext(run_id=run_id):
            logger.info(
                "Reasoning Council starting execution in %s tier for goal: %s",
                tier.value.upper(),
                prompt[:50],
            )

            start_time = time.time()

            # Security scan for Instant tier (or all tiers, kept for parity with old _execute_instant)
            if tier_level == 1:
                firewall = CognitiveFirewall()
                if not firewall.scan_file_for_injection("workspace_context", repo_context)[
                    "is_safe"
                ]:
                    logger.error(
                        "Security: prompt injection detected in workspace context during Instant execution"
                    )
                    raise ValueError(
                        "Security: Potential prompt injection detected in workspace context"
                    )

            target_file = self._extract_target_file(prompt)
            style_profile = await self._get_or_refresh_style_profile(target_file)

            # Resolve format instructions for the active coder model family
            _coder_format_instructions = ""
            try:
                from velune.execution.edit_formats.registry import format_instructions_for
                from velune.models.family import detect_family

                _roles = self.mapper.map_roles()
                from velune.models.specializations import CouncilRole

                _coder_model_desc = _roles.get(CouncilRole.CODER)
                if _coder_model_desc:
                    _family = detect_family(_coder_model_desc.model_id)
                    _coder_format_instructions = format_instructions_for(_family)
            except Exception as _fe:
                logger.debug("Could not resolve edit format instructions: %s", _fe)

            # Agent instantiation based on tier
            coder = self.agent_factory.create_coder(run_id)
            planner = self.agent_factory.create_planner(run_id) if tier_level >= 2 else None
            reviewer = self.agent_factory.create_reviewer(run_id) if tier_level >= 3 else None
            synthesizer = self.agent_factory.create_synthesizer(run_id) if tier_level >= 3 else None

            challenger = self.agent_factory.create_challenger(run_id) if tier_level == 4 else None
            scalability_critic = (
                self.agent_factory.create_scalability_critic(run_id) if tier_level >= 3 else None
            )
            security_critic = (
                self.agent_factory.create_security_critic(run_id) if tier_level >= 2 else None
            )
            performance_critic = (
                self.agent_factory.create_performance_critic(run_id) if tier_level >= 3 else None
            )
            maintainability_critic = (
                self.agent_factory.create_maintainability_critic(run_id)
                if tier_level >= 2
                else None
            )

            # Emit model assignment event so the REPL can show which model handles each role.
            try:
                _role_map = self.agent_factory.get_role_mapping(run_id)
                _assignment_str = "  |  ".join(
                    f"{_role.value}: {_desc.model_id}" for _role, _desc in _role_map.items()
                )
                if progress_callback is not None:
                    progress_callback(f"[Model Assignment] {_assignment_str}")
            except Exception:
                pass

            # Enriched Context for Full Tier
            enriched_repo_context = repo_context
            shi = None
            if tier_level == 4:
                architectural_context = ""
                if self._is_structural_change(prompt, repo_context) and (
                    "class " in repo_context or "def " in repo_context
                ):
                    audit_res = self.architecture_agent.audit_architecture(
                        target_file, repo_context
                    )
                    shi = audit_res.get("shi")
                    debt_items = self.architecture_agent.ledger.get_items()
                    if debt_items:
                        architectural_context += "\n--- KNOWN ARCHITECTURAL DEBT & VIOLATIONS ---\n"
                        for item in debt_items:
                            architectural_context += f"- [{item['category'].upper()}] in '{item['file_path']}': {item['description']} (Severity: {item['severity']})\n"
                        architectural_context += "Please ensure the proposed code fixes or avoids increasing this technical debt.\n"
                        layering_violations = [
                            item for item in debt_items if item["category"] == "layering"
                        ]
                        if layering_violations:
                            architectural_context += (
                                "\n==================================================\n"
                            )
                            architectural_context += (
                                "!!! ARCHITECTURE DRIFT ALARM (ADA) ACTIVE BLOCK !!!\n"
                            )
                            architectural_context += "The following layering boundary violations MUST BE RESOLVED IMMEDIATELY:\n"
                            for item in layering_violations:
                                architectural_context += f"- BLOCKING DRIFT: {item['description']} (File: {item['file_path']})\n"
                            architectural_context += "You MUST plan to fix these import boundary violations in this execution pass.\n"
                            architectural_context += (
                                "==================================================\n\n"
                            )

                decisions, failures = (
                    await self.lineage_memory.query_continuity_warnings(prompt, repo_context)
                    if self.lineage_memory is not None
                    else ([], [])
                )
                continuity_context = ""
                if decisions or failures:
                    continuity_context += "\n--- COGNITIVE CONTINUITY WARNINGS ---\n"
                    if decisions:
                        continuity_context += "\n[DLS] Approved Architectural Decisions:\n"
                        for dec in decisions:
                            continuity_context += f"- Decision {dec['id']}: {dec['rationale']} (Subsystem: {dec['target_subsystem']}, Impact: {dec['architectural_impact']})\n"
                            if dec.get("alternatives"):
                                continuity_context += "  Design Alternatives:\n"
                                for alt in dec["alternatives"]:
                                    continuity_context += f"    * {alt['option_name']}: Tradeoffs {alt['tradeoffs']} (Rejected because: {alt['rejected_reason']})\n"
                    if failures:
                        continuity_context += "\n[FEL] BLOCK: Prior Failed Experiments (Avoid repeating these approaches):\n"
                        for fail in failures:
                            continuity_context += f"- Failed Experiment {fail['id']} in Subsystem: {fail['target_subsystem']}\n"
                            continuity_context += f"  Approach / Patch:\n{fail['patch']}\n"
                            continuity_context += (
                                f"  Failure Error ({fail['error_type']}): {fail['error_message']}\n"
                            )

                firewall = CognitiveFirewall()
                architectural_context = firewall.wrap_workspace_content(
                    "architectural_debt_ledger", architectural_context
                )
                continuity_context = firewall.wrap_workspace_content(
                    "continuity_warnings", continuity_context
                )
                enriched_repo_context = repo_context + architectural_context + continuity_context

            # 1. Planner Phase
            task_plan = None
            plan_desc = "Direct implementation (Instant path chosen)."
            if planner:
                logger.info("Council Phase: Planner")
                if progress_callback:
                    progress_callback("[Planner] Decomposing task...")
                try:
                    task_plan = await asyncio.wait_for(
                        planner.generate_plan(prompt, enriched_repo_context),
                        timeout=budget.planner_timeout_seconds,
                    )
                except TimeoutError:
                    logger.error("Planner timed out after %ds", budget.planner_timeout_seconds)
                    task_plan = None
                plan_desc = (
                    "\n".join([f"- {s.id}: {s.description}" for s in task_plan.steps])
                    if task_plan
                    else "Direct implementation (Planner timed out)."
                )

            # 2. Coder Phase (Round 1 — Diverge: multi-solver self-consistency)
            logger.info("Council Phase: Coder")
            if progress_callback:
                progress_callback("[Coder] Designing code implementation...")

            from velune.cognition.consensus import medoid_index
            from velune.cognition.council.sampling import coder_sample_count
            from velune.models.specializations import detect_model_collapse

            coder_low_resource = (
                self.config and self.config.execution.low_resource_mode
            ) or os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")

            degraded_diversity = False
            try:
                role_mapping = self.agent_factory.get_role_mapping(run_id)
                degraded_diversity = detect_model_collapse(role_mapping)
            except Exception:
                pass

            # Only the deliberative tiers (>=3) draw multiple candidates; instant
            # and minimal stay single-shot for latency.
            n_samples = (
                coder_sample_count(coder_low_resource, degraded_diversity) if tier_level >= 3 else 1
            )

            candidate_pool = await self._diverge_candidates(
                coder,
                prompt=prompt,
                current_code=enriched_repo_context,
                plan_context=plan_desc,
                style_profile=style_profile,
                format_instructions=_coder_format_instructions,
                n_samples=n_samples,
                timeout=budget.coder_timeout_seconds,
                progress_callback=progress_callback,
            )
            if not candidate_pool:
                logger.error("Coder produced no usable candidates (all failed/timed out)")
                return self._build_timeout_result(tier, start_time)

            winner_idx = medoid_index(candidate_pool) if len(candidate_pool) > 1 else 0
            coder_proposal = candidate_pool[winner_idx]
            if len(candidate_pool) > 1:
                logger.info(
                    "Coder diverge round: %d candidates, self-consistency winner #%d (degraded_diversity=%s, mode=%s)",
                    len(candidate_pool),
                    winner_idx + 1,
                    degraded_diversity,
                    self.scheduler.last_mode,
                )
                if progress_callback:
                    progress_callback(
                        f"[Coder] Self-consistency vote: selected candidate {winner_idx + 1}/{len(candidate_pool)}"
                    )

            # Early Return for Instant/Minimal
            if tier_level < 3:
                arbitration_dict = {"overall_confidence": 0.85, "requires_human_review": False}
                if tier_level == 2 and task_plan:
                    if progress_callback:
                        progress_callback("[Arbitration] Arbitrating proposal...")
                    arbitration = self.arbitrator.arbitrate(
                        plan_steps=[s.description for s in task_plan.steps],
                        coder_proposal=coder_proposal,
                        reviewer_report=None,
                        challenger_report=None,
                        scalability_report=None,
                        security_report=None,
                        performance_report=None,
                        maintainability_report=None,
                    )
                    arbitration_dict = arbitration.to_dict()

                _est_prompt = len(prompt.encode()) // 4
                _est_completion = len(coder_proposal.encode()) // 4
                _est_total = _est_prompt + _est_completion
                if progress_callback:
                    progress_callback(f"[Usage] ~{_est_total:,} tokens across 1 agent (estimated)")

                logger.info("Executed %s tier in %.2fs", tier.value, time.time() - start_time)
                return {
                    "tier": tier.value,
                    "task_plan": task_plan,
                    "coder_proposal": coder_proposal,
                    "reviewer_report": None,
                    "challenger_report": None,
                    "arbitration": arbitration_dict,
                    "final_summary": coder_proposal,
                }

            # 3. Review / Critique Phase
            logger.info("Council Phase: Reviewer/Critics")
            if progress_callback:
                progress_callback("[Reviewer] Running review and critique...")

            reviewer_report = ReviewerMessage(
                passed=True, confidence_rating=0.5, critical_issues=[]
            )
            challenger_report = ChallengerMessage(
                assumptions_challenged=[], failure_vectors=[], severity_rating=0.0
            )
            scalability_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")
            security_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")
            performance_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")
            maintainability_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")

            tasks = [reviewer.review(task=prompt, proposal=coder_proposal, context=repo_context)]
            _task_idx: dict[str, int] = {}
            _next = 1
            if challenger:
                tasks.append(
                    challenger.challenge(task=prompt, proposal=coder_proposal, context=repo_context)
                )
                _task_idx["challenger"] = _next
                _next += 1
            if scalability_critic:
                tasks.append(
                    scalability_critic.critique(
                        task=prompt, proposal=coder_proposal, context=repo_context
                    )
                )
                _task_idx["scalability"] = _next
                _next += 1
            if security_critic:
                tasks.append(
                    security_critic.critique(
                        task=prompt, proposal=coder_proposal, context=repo_context
                    )
                )
                _task_idx["security"] = _next
                _next += 1
            if performance_critic:
                tasks.append(
                    performance_critic.critique(
                        task=prompt, proposal=coder_proposal, context=repo_context
                    )
                )
                _task_idx["performance"] = _next
                _next += 1
            if maintainability_critic:
                tasks.append(
                    maintainability_critic.critique(
                        task=prompt, proposal=coder_proposal, context=repo_context
                    )
                )
                _task_idx["maintainability"] = _next
                _next += 1

            results = await asyncio.gather(*tasks, return_exceptions=True)

            if not isinstance(results[0], Exception):
                reviewer_report = results[0]
            else:
                logger.error("Reviewer failed: %s", results[0])
                reviewer_report.critical_issues = ["Reviewer unavailable"]

            def _pick(key: str, default):
                i = _task_idx.get(key)
                if i is None:
                    return default
                r = results[i]
                if isinstance(r, Exception):
                    logger.error("%s critic failed: %s", key, r)
                    return default
                return r

            challenger_report = _pick("challenger", challenger_report)
            scalability_report = _pick("scalability", scalability_report)
            security_report = _pick("security", security_report)
            performance_report = _pick("performance", performance_report)
            maintainability_report = _pick("maintainability", maintainability_report)

            # 4. Debate Phase
            objections = []
            if not reviewer_report.passed:
                objections.append(f"Reviewer: {reviewer_report.critical_issues}")
            if not scalability_report.passed:
                objections.append(f"Scalability Critic: {scalability_report.issues}")
            if not security_report.passed:
                objections.append(f"Security Critic: {security_report.issues}")
            if not performance_report.passed:
                objections.append(f"Performance Critic: {performance_report.issues}")
            if not maintainability_report.passed:
                objections.append(f"Maintainability Critic: {maintainability_report.issues}")
            if challenger_report.severity_rating > 0.6:
                objections.append(
                    f"Challenger (Severity: {challenger_report.severity_rating}): {challenger_report.failure_vectors}"
                )

            low_resource = (
                self.config and self.config.execution.low_resource_mode
            ) or os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")

            max_debate_turns = 0
            if tier_level == 3 and objections and not low_resource:
                max_debate_turns = min(1, budget.max_review_cycles)
            elif tier_level == 4:
                all_critic_reports = {
                    "security": security_report,
                    "scalability": scalability_report,
                    "challenger": challenger_report,
                }
                max_debate_turns = calculate_max_debate_turns(
                    initial_objections=objections,
                    critic_reports=all_critic_reports,
                    task_complexity="structural",
                )
                max_debate_turns = min(budget.max_review_cycles, max_debate_turns)

            refined_proposal = coder_proposal
            initial_objection_count = len(objections)
            converged = initial_objection_count == 0
            turns_required = 0
            debate_start_time = time.time()

            if objections and max_debate_turns > 0:
                logger.info("[COUNCIL - DEBATE] Objections detected. Initiating Debate Loop...")

                async def _run_debate_loop() -> None:
                    nonlocal \
                        coder_proposal, \
                        refined_proposal, \
                        reviewer_report, \
                        scalability_report, \
                        security_report, \
                        performance_report, \
                        maintainability_report, \
                        objections, \
                        converged, \
                        turns_required

                    debate_turn = 1
                    refined_proposal = coder_proposal

                    while debate_turn <= max_debate_turns:
                        elapsed = time.time() - start_time
                        if elapsed >= budget.max_wall_time_seconds:
                            logger.warning(
                                "[COUNCIL - DEBATE] Wall-time budget exhausted (%.1fs >= %ds); stopping debate.",
                                elapsed,
                                budget.max_wall_time_seconds,
                            )
                            break

                        logger.info(
                            "[COUNCIL - DEBATE] Debate Loop Turn %d/%d",
                            debate_turn,
                            max_debate_turns,
                        )
                        if progress_callback:
                            progress_callback(f"[Debate] Running debate turn {debate_turn}...")
                        turns_required = debate_turn

                        objections_text = "\n".join([f"- {obj}" for obj in objections])
                        refine_prompt = (
                            f"The Reasoning Council has raised the following objections to your previous proposal:\n"
                            f"{objections_text}\n\n"
                            f"Please rewrite and refine the proposed code to resolve ALL of these objections completely while satisfying the original task."
                        )

                        try:
                            refined_proposal = await asyncio.wait_for(
                                coder.write_code(
                                    prompt=prompt,
                                    current_code=enriched_repo_context,
                                    plan_context=f"Debate Refinement (Turn {debate_turn}):\n{refine_prompt}",
                                    style_profile=style_profile,
                                    format_instructions=_coder_format_instructions,
                                ),
                                timeout=budget.coder_timeout_seconds,
                            )
                        except TimeoutError:
                            logger.error(
                                "[COUNCIL - DEBATE] Coder timed out on turn %d after %ds; stopping debate.",
                                debate_turn,
                                budget.coder_timeout_seconds,
                            )
                            break

                        re_tasks = []
                        re_critics = []

                        if not reviewer_report.passed:
                            re_tasks.append(
                                reviewer.review(
                                    task=prompt, proposal=refined_proposal, context=repo_context
                                )
                            )
                            re_critics.append("reviewer")
                        if tier_level == 4:
                            if not scalability_report.passed:
                                re_tasks.append(
                                    scalability_critic.critique(
                                        task=prompt, proposal=refined_proposal, context=repo_context
                                    )
                                )
                                re_critics.append("scalability")
                            if not security_report.passed:
                                re_tasks.append(
                                    security_critic.critique(
                                        task=prompt, proposal=refined_proposal, context=repo_context
                                    )
                                )
                                re_critics.append("security")
                            if not performance_report.passed:
                                re_tasks.append(
                                    performance_critic.critique(
                                        task=prompt, proposal=refined_proposal, context=repo_context
                                    )
                                )
                                re_critics.append("performance")
                            if not maintainability_report.passed:
                                re_tasks.append(
                                    maintainability_critic.critique(
                                        task=prompt, proposal=refined_proposal, context=repo_context
                                    )
                                )
                                re_critics.append("maintainability")

                        if re_tasks:
                            raw_re_results = await asyncio.gather(*re_tasks, return_exceptions=True)
                            re_results = []
                            for name, res in zip(re_critics, raw_re_results, strict=False):
                                if isinstance(res, Exception):
                                    if name == "reviewer":
                                        res = ReviewerMessage(
                                            passed=True,
                                            confidence_rating=0.5,
                                            critical_issues=[
                                                "Reviewer unavailable during refinement"
                                            ],
                                        )
                                    else:
                                        res = CriticMessage(
                                            passed=True,
                                            issues=[f"{name} unavailable during refinement"],
                                            score=0.9,
                                            rationale="",
                                        )
                                re_results.append(res)

                            for name, res in zip(re_critics, re_results, strict=False):
                                if name == "reviewer":
                                    reviewer_report = res
                                elif name == "scalability":
                                    scalability_report = res
                                elif name == "security":
                                    security_report = res
                                elif name == "performance":
                                    performance_report = res
                                elif name == "maintainability":
                                    maintainability_report = res

                            all_passed_with_high_score = True
                            for name, res in zip(re_critics, re_results, strict=False):
                                score = (
                                    res.confidence_rating
                                    if name == "reviewer"
                                    else getattr(res, "score", 1.0)
                                )
                                if not res.passed or score <= 0.8:
                                    all_passed_with_high_score = False
                                    break

                            if all_passed_with_high_score:
                                coder_proposal = refined_proposal
                                objections = []
                                converged = True
                                break

                            new_objections = []
                            for name, res in zip(re_critics, re_results, strict=False):
                                if not res.passed:
                                    if name == "reviewer":
                                        new_objections.append(f"Reviewer: {res.critical_issues}")
                                    elif name == "scalability":
                                        new_objections.append(f"Scalability Critic: {res.issues}")
                                    elif name == "security":
                                        new_objections.append(f"Security Critic: {res.issues}")
                                    elif name == "performance":
                                        new_objections.append(f"Performance Critic: {res.issues}")
                                    elif name == "maintainability":
                                        new_objections.append(
                                            f"Maintainability Critic: {res.issues}"
                                        )
                            objections = new_objections
                        else:
                            objections = []

                        if not objections:
                            coder_proposal = refined_proposal
                            converged = True
                            break

                        debate_turn += 1

                    if objections:
                        coder_proposal = refined_proposal
                        converged = False

                remaining = max(1.0, budget.max_wall_time_seconds - (time.time() - start_time))
                try:
                    await asyncio.wait_for(_run_debate_loop(), timeout=remaining)
                except TimeoutError:
                    logger.warning("Debate loop hit wall time limit, proceeding with best proposal")
                    converged = False

            if tier_level == 4 and initial_objection_count > 0:
                time_to_converge_ms = int((time.time() - debate_start_time) * 1000)
                self.analytics.record_debate_outcome(
                    turns_required=turns_required,
                    initial_objection_count=initial_objection_count,
                    final_objection_count=len(objections),
                    converged=converged,
                    time_to_converge_ms=time_to_converge_ms,
                )

            # 5. Arbitration Phase
            logger.info("Council Phase: Arbitration")
            if progress_callback:
                progress_callback("[Arbitration] Arbitrating proposal...")
            arbitration = self.arbitrator.arbitrate(
                plan_steps=[s.description for s in task_plan.steps],
                coder_proposal=coder_proposal,
                reviewer_report=reviewer_report,
                challenger_report=challenger_report if tier_level == 4 else None,
                scalability_report=scalability_report if tier_level == 4 else None,
                security_report=security_report if tier_level == 4 else None,
                performance_report=performance_report if tier_level == 4 else None,
                maintainability_report=maintainability_report if tier_level == 4 else None,
                shi=shi,
                candidates=candidate_pool,
            )

            # 6. Synthesis Phase
            logger.info("Council Phase: Synthesis")
            if progress_callback:
                progress_callback("[Synthesis] Synthesizing final summary...")

            audit_reports = [reviewer_report.model_dump()]
            if tier_level == 4:
                audit_reports.extend(
                    [
                        challenger_report.model_dump(),
                        scalability_report.model_dump(),
                        security_report.model_dump(),
                        performance_report.model_dump(),
                        maintainability_report.model_dump(),
                    ]
                )

            try:
                final_summary = await synthesizer.synthesize(
                    task=prompt,
                    winning_claims=arbitration.winning_claims,
                    plan=coder_proposal,
                    audit_reports=audit_reports,
                    context=repo_context,
                )
                if final_summary.startswith("Deliberation failure inside agent"):
                    raise ValueError(final_summary)
            except Exception as e:
                logger.error("Synthesizer failed: %s. Using default fallback summary.", e)
                final_summary = (
                    f"# Council Deliberation Report (Degraded Mode)\n\n"
                    f"The Lead Synthesizer was unavailable due to an unexpected offline model or network issue ({e}).\n"
                    f"Below is the raw compiled code proposal and findings directly from the deliberation council:\n\n"
                    f"## Task\n{prompt}\n\n"
                    f"## Code Proposal\n```\n{coder_proposal}\n```\n\n"
                    f"## Arbitration Status\n- **Confidence**: {arbitration.overall_confidence}\n- **Requires Human Review**: {arbitration.requires_human_review}\n- **Instructions**: {arbitration.synthesis_instructions}\n"
                )

            # 7. Lineage Logging
            decision_id = f"DEC-{int(time.time())}"
            impact = self.estimate_blast_radius(target_file)

            if self.lineage_memory is not None and tier_level == 3:
                await self.lineage_memory.log_decision(
                    decision_id=decision_id,
                    target_subsystem="standard_path",
                    rationale=final_summary[:300],
                    architectural_impact=impact,
                    consequences="Standard execution completed with single-reviewer arbitration.",
                )
            elif self.lineage_memory is not None and tier_level == 4:
                subsystem_target = target_file
                for key in [
                    "database",
                    "concurrency",
                    "lock",
                    "thread",
                    "async",
                    "cache",
                    "sandbox",
                    "security",
                    "telemetry",
                    "model",
                    "routing",
                    "memory",
                ]:
                    if key in prompt.lower():
                        subsystem_target = key
                        break

                if objections:
                    await self.lineage_memory.log_failed_experiment(
                        target_subsystem=subsystem_target,
                        patch=coder_proposal[:1000],
                        error_type="critic_veto",
                        error_message=f"Debate loop finished but objections remained: {objections}",
                    )
                elif arbitration.requires_human_review:
                    await self.lineage_memory.log_failed_experiment(
                        target_subsystem=subsystem_target,
                        patch=coder_proposal[:1000],
                        error_type="arbitration_veto",
                        error_message="Reasoning council arbitration vetoed proposal due to low confidence.",
                    )
                else:
                    await self.lineage_memory.log_decision(
                        decision_id=decision_id,
                        target_subsystem=subsystem_target,
                        rationale=final_summary[:300],
                        architectural_impact=impact,
                        consequences="Approved and validated by Reasoning Council.",
                        alternatives=[
                            {
                                "option_name": "Proposed Solution",
                                "tradeoffs": {"confidence": arbitration.overall_confidence},
                                "rejected_reason": "",
                            }
                        ],
                    )

            # Token usage summary — estimate from text lengths since agent
            # responses are strings, not InferenceResponse objects.
            _agent_texts = [
                t
                for t in [
                    coder_proposal,
                    final_summary,
                    getattr(reviewer_report, "model_dump", lambda: {})().get("critical_issues", ""),
                ]
                if t
            ]
            _total_prompt = len(prompt.encode()) // 4
            _total_completion = sum(len(str(t).encode()) // 4 for t in _agent_texts)
            _agent_count = 1 + (1 if tier_level >= 3 else 0) + (4 if tier_level == 4 else 0)
            if progress_callback:
                progress_callback(
                    f"[Usage] ~{_total_prompt + _total_completion:,} tokens "
                    f"across {_agent_count} agents (estimated)"
                )

            logger.info("Executed %s tier in %.2fs", tier.value, time.time() - start_time)
            return {
                "tier": tier.value,
                "task_plan": task_plan,
                "coder_proposal": coder_proposal,
                "reviewer_report": reviewer_report,
                "challenger_report": challenger_report if tier_level == 4 else None,
                "scalability_report": scalability_report if tier_level == 4 else None,
                "security_report": security_report if tier_level == 4 else None,
                "performance_report": performance_report if tier_level == 4 else None,
                "maintainability_report": maintainability_report if tier_level == 4 else None,
                "arbitration": arbitration.to_dict(),
                "final_summary": final_summary,
            }
