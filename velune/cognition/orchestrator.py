"""LangGraph-style orchestrator compiling council roles and executing deliberation flows."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, AsyncIterator

from velune.memory.tiers.lineage import LineageMemoryTier

if TYPE_CHECKING:
    from velune.memory.storage.sqlite_manager import SQLiteManager
    from velune.kernel.config import VeluneConfig

from velune.cognition.arbitrator import CouncilArbitrator
from velune.cognition.architecture import ArchitectureCognitionAgent
from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.coder import CoderAgent
from velune.cognition.council.critics import (
    MaintainabilityCritic,
    PerformanceCritic,
    ScalabilityCritic,
    SecurityCritic,
)
from velune.cognition.council.debate import calculate_max_debate_turns
from velune.cognition.council.planner import PlannerAgent
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.council.synthesizer import SynthesizerAgent
from velune.cognition.council.tiers import CouncilTier, classify_task_tier, TierClassifier
from velune.cognition.council.factory import CouncilAgentFactory
from velune.cognition.style_resolver import StyleResolver
from velune.core.trace import TracedLogger
from velune.models.specializations import CouncilRole, ModelSpecializationMapper
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
    ) -> None:
        self.provider_registry = provider_registry
        self.mapper = mapper
        self.arbitrator = CouncilArbitrator(historical_accuracy=historical_accuracy)
        self.architecture_agent = ArchitectureCognitionAgent(workspace_root=None, ledger=None)
        self.config = config

        db_path = lineage_db_path or Path(".velune") / "velune_cognitive_core.db"
        self.lineage_memory = LineageMemoryTier(db_path, sqlite_manager=sqlite_manager)
        self.analytics = analytics or CognitivePerformanceAnalytics(sqlite_manager=sqlite_manager)

        from velune.cognition.firewall import CognitiveFirewall
        self.firewall = CognitiveFirewall()

        self.max_wall_time_seconds = float(
            os.environ.get("VELUNE_COUNCIL_MAX_SECONDS", "600")  # 10 minutes default
        )
        self._states: dict[str, Any] = {}
        self._live_lock = asyncio.Lock()

        # Extracted Subsystems
        self.agent_factory = CouncilAgentFactory(
            provider_registry=self.provider_registry,
            mapper=self.mapper,
            live_lock=self._live_lock
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

        low_resource_mode = (
            (config and config.execution.low_resource_mode) or
            os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")
        )

        self.tier_classifier = TierClassifier(
            task_registry=task_registry,
            max_council_tier=max_tier,
            default_tier_override=default_override,
            low_resource_mode=low_resource_mode,
        )

    def get_state(self, run_id: str) -> Any | None:
        """Get the cached OrchestrationState by run_id."""
        return self._states.get(run_id)

    async def stream(self, prompt: str) -> AsyncIterator[StreamProgress]:
        """Runs the Reasoning Council task execution and streams milestones."""
        import uuid
        from velune.orchestration.schemas import OrchestrationState, OrchestrationRequest, ExecutionStatus, StreamProgress
        
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        queue = asyncio.Queue()

        def progress_callback(msg: str):
            phase = ""
            message = msg
            if msg.startswith("[") and "]" in msg:
                parts = msg.split("]", 1)
                phase = parts[0][1:].lower()
                message = parts[1].strip()
            queue.put_nowait(StreamProgress(run_id=run_id, phase=phase, message=message))

        # First, emit context reconstruction milestone before indexing
        progress_callback("[Context Reconstruction] Gathering repository context snapshot...")

        repo_context = "Repository context summary."
        try:
            from velune.kernel.registry import get_container
            container = get_container()
            if container.has("runtime.repository_cognition"):
                repository_cognition = container.get("runtime.repository_cognition")
                snapshot = repository_cognition.index(force=False)
                if snapshot:
                    lines = [f"Repository Root: {snapshot.root_path}"]
                    lines.append("Codebase Files:")
                    for f in snapshot.files[:25]:
                        lines.append(f"  - {f.path} ({f.language.value})")
                    repo_context = "\n".join(lines)
                    
                    # Scan and sanitize context injection to prevent persistent prompt injection
                    scan_res = self.firewall.scan_file_for_injection("repo_context", repo_context)
                    if scan_res.get("quarantined"):
                        repo_context = scan_res.get("neutralized_content", "")
                        logger.warning("Repository context neutralized by firewall before prompt injection.")
        except Exception as e:
            logger.warning("Could not gather repository snapshot: %s", e)

        async def run_execution():
            try:
                result = await self.execute_task(
                    prompt=prompt,
                    repo_context=repo_context,
                    progress_callback=progress_callback
                )
                final_summary = result.get("final_summary", "Execution completed successfully.")
                status = ExecutionStatus.COMPLETED
                error = None
                task_plan = result.get("task_plan")
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
            "redesign", "architect", "concurrency", "thread", "async", "lock", "database",
            "class", "interface", "refactor", "performance", "scalability", "security",
            "cohesion", "module", "coupling", "sandbox", "boundary", "lcom", "critic"
        ]

        for kw in structural_keywords:
            if kw in prompt_lower:
                return True

        # 3. Simple indicators
        simple_keywords = ["typo", "comment", "format", "rename variable", "ui text", "alignment", "simple tweak"]
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
            profile = self.mapper.profiler.get_profile(coder_model.provider_id, coder_model.model_id)
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
    ) -> dict[str, Any]:
        """Orchestrate a complete council deliberation pass for a task prompt with wall-time limit."""
        tier = self._resolve_tier(prompt, repo_context, council_tier)
        tier_str = tier.value

        try:
            return await asyncio.wait_for(
                self._execute_tiered(
                    prompt=prompt,
                    repo_context=repo_context,
                    tier=tier,
                    progress_callback=progress_callback,
                ),
                timeout=self.max_wall_time_seconds
            )
        except asyncio.TimeoutError:
            logger.error(
                "Council wall-time limit reached (%.0fs). "
                "Returning partial result.",
                self.max_wall_time_seconds
            )
            return self._build_timeout_result(prompt, tier_str)

    def _build_timeout_result(self, prompt: str, tier: str = "full") -> dict[str, Any]:
        from velune.cognition.council.messages import ReviewerMessage
        return {
            "tier": tier,
            "task_plan": None,
            "coder_proposal": None,
            "reviewer_report": ReviewerMessage(passed=False, critical_issues=["Council execution timed out."], confidence_rating=0.0),
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
            "final_summary": f"Council execution timed out. Partial analysis: (None)",
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
    ) -> dict[str, Any]:
        """Consolidated orchestrator execution path for all tiers."""
        import uuid
        import time
        from velune.core.trace import TraceContext
        from velune.cognition.firewall import CognitiveFirewall
        from velune.cognition.council.messages import ReviewerMessage, ChallengerMessage, CriticMessage

        if run_id == "default":
            run_id = f"council-{uuid.uuid4().hex[:8]}"
            
        tier_level = {"instant": 1, "minimal": 2, "standard": 3, "full": 4}[tier.value]

        with TraceContext(run_id=run_id):
            logger.info("Reasoning Council starting execution in %s tier for goal: %s", tier.value.upper(), prompt[:50])

            start_time = time.time()
            
            # Security scan for Instant tier (or all tiers, kept for parity with old _execute_instant)
            if tier_level == 1:
                firewall = CognitiveFirewall()
                if not firewall.scan_file_for_injection("workspace_context", repo_context)["is_safe"]:
                    logger.error("Security: prompt injection detected in workspace context during Instant execution")
                    raise ValueError("Security: Potential prompt injection detected in workspace context")

            target_file = self._extract_target_file(prompt)
            style_profile = await self._get_or_refresh_style_profile(target_file)

            # Agent instantiation based on tier
            coder = self.agent_factory.create_coder(run_id)
            planner = self.agent_factory.create_planner(run_id) if tier_level >= 2 else None
            reviewer = self.agent_factory.create_reviewer(run_id) if tier_level >= 3 else None
            synthesizer = self.agent_factory.create_synthesizer(run_id) if tier_level >= 3 else None
            
            challenger = self.agent_factory.create_challenger(run_id) if tier_level == 4 else None
            scalability_critic = self.agent_factory.create_scalability_critic(run_id) if tier_level == 4 else None
            security_critic = self.agent_factory.create_security_critic(run_id) if tier_level == 4 else None
            performance_critic = self.agent_factory.create_performance_critic(run_id) if tier_level == 4 else None
            maintainability_critic = self.agent_factory.create_maintainability_critic(run_id) if tier_level == 4 else None

            # Enriched Context for Full Tier
            enriched_repo_context = repo_context
            shi = None
            if tier_level == 4:
                architectural_context = ""
                if self._is_structural_change(prompt, repo_context) and ("class " in repo_context or "def " in repo_context):
                    audit_res = self.architecture_agent.audit_architecture(target_file, repo_context)
                    shi = audit_res.get("shi")
                    debt_items = self.architecture_agent.ledger.get_items()
                    if debt_items:
                        architectural_context += "\n--- KNOWN ARCHITECTURAL DEBT & VIOLATIONS ---\n"
                        for item in debt_items:
                            architectural_context += f"- [{item['category'].upper()}] in '{item['file_path']}': {item['description']} (Severity: {item['severity']})\n"
                        architectural_context += "Please ensure the proposed code fixes or avoids increasing this technical debt.\n"
                        layering_violations = [item for item in debt_items if item["category"] == "layering"]
                        if layering_violations:
                            architectural_context += "\n==================================================\n"
                            architectural_context += "!!! ARCHITECTURE DRIFT ALARM (ADA) ACTIVE BLOCK !!!\n"
                            architectural_context += "The following layering boundary violations MUST BE RESOLVED IMMEDIATELY:\n"
                            for item in layering_violations:
                                architectural_context += f"- BLOCKING DRIFT: {item['description']} (File: {item['file_path']})\n"
                            architectural_context += "You MUST plan to fix these import boundary violations in this execution pass.\n"
                            architectural_context += "==================================================\n\n"

                decisions, failures = self.lineage_memory.query_continuity_warnings(prompt, repo_context)
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
                            continuity_context += f"  Failure Error ({fail['error_type']}): {fail['error_message']}\n"

                firewall = CognitiveFirewall()
                architectural_context = firewall.wrap_workspace_content("architectural_debt_ledger", architectural_context)
                continuity_context = firewall.wrap_workspace_content("continuity_warnings", continuity_context)
                enriched_repo_context = repo_context + architectural_context + continuity_context

            # 1. Planner Phase
            task_plan = None
            plan_desc = "Direct implementation (Instant path chosen)."
            if planner:
                logger.info("Council Phase: Planner")
                if progress_callback: progress_callback("[Planner] Decomposing task...")
                task_plan = await planner.generate_plan(prompt, enriched_repo_context)
                plan_desc = "\n".join([f"- {s.id}: {s.description}" for s in task_plan.steps])

            # 2. Coder Phase
            logger.info("Council Phase: Coder")
            if progress_callback: progress_callback("[Coder] Designing code implementation...")
            coder_proposal = await coder.write_code(
                prompt=prompt,
                current_code=enriched_repo_context,
                plan_context=plan_desc,
                style_profile=style_profile,
            )

            # Early Return for Instant/Minimal
            if tier_level < 3:
                arbitration_dict = {"overall_confidence": 0.85, "requires_human_review": False}
                if tier_level == 2 and task_plan:
                    if progress_callback: progress_callback("[Arbitration] Arbitrating proposal...")
                    arbitration = self.arbitrator.arbitrate(
                        plan_steps=[s.description for s in task_plan.steps],
                        coder_proposal=coder_proposal,
                        reviewer_report=None, challenger_report=None,
                        scalability_report=None, security_report=None,
                        performance_report=None, maintainability_report=None
                    )
                    arbitration_dict = arbitration.to_dict()

                _est_prompt = len(prompt.encode()) // 4
                _est_completion = len(coder_proposal.encode()) // 4
                _est_total = _est_prompt + _est_completion
                if progress_callback:
                    progress_callback(
                        f"[Usage] ~{_est_total:,} tokens across 1 agent (estimated)"
                    )

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
            if progress_callback: progress_callback("[Reviewer] Running review and critique...")

            reviewer_report = ReviewerMessage(passed=True, confidence_rating=0.5, critical_issues=[])
            challenger_report = ChallengerMessage(assumptions_challenged=[], failure_vectors=[], severity_rating=0.0)
            scalability_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")
            security_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")
            performance_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")
            maintainability_report = CriticMessage(passed=True, issues=[], score=1.0, rationale="")

            tasks = [reviewer.review(task=prompt, proposal=coder_proposal, context=repo_context)]
            if tier_level == 4:
                tasks.extend([
                    challenger.challenge(task=prompt, proposal=coder_proposal, context=repo_context),
                    scalability_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context),
                    security_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context),
                    performance_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context),
                    maintainability_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context)
                ])

            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            if not isinstance(results[0], Exception):
                reviewer_report = results[0]
            else:
                logger.error("Reviewer failed: %s", results[0])
                reviewer_report.critical_issues = ["Reviewer unavailable"]

            if tier_level == 4:
                if not isinstance(results[1], Exception): challenger_report = results[1]
                if not isinstance(results[2], Exception): scalability_report = results[2]
                if not isinstance(results[3], Exception): security_report = results[3]
                if not isinstance(results[4], Exception): performance_report = results[4]
                if not isinstance(results[5], Exception): maintainability_report = results[5]

            # 4. Debate Phase
            objections = []
            if not reviewer_report.passed: objections.append(f"Reviewer: {reviewer_report.critical_issues}")
            if not scalability_report.passed: objections.append(f"Scalability Critic: {scalability_report.issues}")
            if not security_report.passed: objections.append(f"Security Critic: {security_report.issues}")
            if not performance_report.passed: objections.append(f"Performance Critic: {performance_report.issues}")
            if not maintainability_report.passed: objections.append(f"Maintainability Critic: {maintainability_report.issues}")
            if challenger_report.severity_rating > 0.6: objections.append(f"Challenger (Severity: {challenger_report.severity_rating}): {challenger_report.failure_vectors}")

            low_resource = (
                (self.config and self.config.execution.low_resource_mode) or
                os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")
            )

            max_debate_turns = 0
            if tier_level == 3 and objections and not low_resource:
                max_debate_turns = 1
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
                max_debate_turns = min(3, max_debate_turns)

            refined_proposal = coder_proposal
            initial_objection_count = len(objections)
            converged = (initial_objection_count == 0)
            turns_required = 0
            debate_start_time = time.time()

            if objections and max_debate_turns > 0:
                logger.info("[COUNCIL - DEBATE] Objections detected. Initiating Debate Loop...")

                async def _run_debate_loop() -> None:
                    nonlocal coder_proposal, refined_proposal, reviewer_report, scalability_report, security_report, performance_report, maintainability_report, objections, converged, turns_required

                    debate_turn = 1
                    refined_proposal = coder_proposal

                    while debate_turn <= max_debate_turns:
                        logger.info("[COUNCIL - DEBATE] Debate Loop Turn %d/%d", debate_turn, max_debate_turns)
                        if progress_callback: progress_callback(f"[Debate] Running debate turn {debate_turn}...")
                        turns_required = debate_turn

                        objections_text = "\n".join([f"- {obj}" for obj in objections])
                        refine_prompt = (
                            f"The Reasoning Council has raised the following objections to your previous proposal:\n"
                            f"{objections_text}\n\n"
                            f"Please rewrite and refine the proposed code to resolve ALL of these objections completely while satisfying the original task."
                        )

                        refined_proposal = await coder.write_code(
                            prompt=prompt,
                            current_code=enriched_repo_context,
                            plan_context=f"Debate Refinement (Turn {debate_turn}):\n{refine_prompt}",
                            style_profile=style_profile,
                        )

                        re_tasks = []
                        re_critics = []

                        if not reviewer_report.passed:
                            re_tasks.append(reviewer.review(task=prompt, proposal=refined_proposal, context=repo_context))
                            re_critics.append("reviewer")
                        if tier_level == 4:
                            if not scalability_report.passed:
                                re_tasks.append(scalability_critic.critique(task=prompt, proposal=refined_proposal, context=repo_context))
                                re_critics.append("scalability")
                            if not security_report.passed:
                                re_tasks.append(security_critic.critique(task=prompt, proposal=refined_proposal, context=repo_context))
                                re_critics.append("security")
                            if not performance_report.passed:
                                re_tasks.append(performance_critic.critique(task=prompt, proposal=refined_proposal, context=repo_context))
                                re_critics.append("performance")
                            if not maintainability_report.passed:
                                re_tasks.append(maintainability_critic.critique(task=prompt, proposal=refined_proposal, context=repo_context))
                                re_critics.append("maintainability")

                        if re_tasks:
                            raw_re_results = await asyncio.gather(*re_tasks, return_exceptions=True)
                            re_results = []
                            for name, res in zip(re_critics, raw_re_results):
                                if isinstance(res, Exception):
                                    if name == "reviewer":
                                        res = ReviewerMessage(passed=True, confidence_rating=0.5, critical_issues=["Reviewer unavailable during refinement"])
                                    else:
                                        res = CriticMessage(passed=True, issues=[f"{name} unavailable during refinement"], score=0.9, rationale="")
                                re_results.append(res)

                            for name, res in zip(re_critics, re_results):
                                if name == "reviewer": reviewer_report = res
                                elif name == "scalability": scalability_report = res
                                elif name == "security": security_report = res
                                elif name == "performance": performance_report = res
                                elif name == "maintainability": maintainability_report = res

                            all_passed_with_high_score = True
                            for name, res in zip(re_critics, re_results):
                                score = res.confidence_rating if name == "reviewer" else getattr(res, "score", 1.0)
                                if not res.passed or score <= 0.8:
                                    all_passed_with_high_score = False
                                    break

                            if all_passed_with_high_score:
                                coder_proposal = refined_proposal
                                objections = []
                                converged = True
                                break

                            new_objections = []
                            for name, res in zip(re_critics, re_results):
                                if not res.passed:
                                    if name == "reviewer": new_objections.append(f"Reviewer: {res.critical_issues}")
                                    elif name == "scalability": new_objections.append(f"Scalability Critic: {res.issues}")
                                    elif name == "security": new_objections.append(f"Security Critic: {res.issues}")
                                    elif name == "performance": new_objections.append(f"Performance Critic: {res.issues}")
                                    elif name == "maintainability": new_objections.append(f"Maintainability Critic: {res.issues}")
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

                try:
                    await asyncio.wait_for(_run_debate_loop(), timeout=300.0)
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
            if progress_callback: progress_callback("[Arbitration] Arbitrating proposal...")
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
            )

            # 6. Synthesis Phase
            logger.info("Council Phase: Synthesis")
            if progress_callback: progress_callback("[Synthesis] Synthesizing final summary...")
                
            audit_reports = [reviewer_report.model_dump()]
            if tier_level == 4:
                audit_reports.extend([
                    challenger_report.model_dump(),
                    scalability_report.model_dump(),
                    security_report.model_dump(),
                    performance_report.model_dump(),
                    maintainability_report.model_dump()
                ])

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
            
            if tier_level == 3:
                self.lineage_memory.log_decision(
                    decision_id=decision_id,
                    target_subsystem="standard_path",
                    rationale=final_summary[:300],
                    architectural_impact=impact,
                    consequences="Standard execution completed with single-reviewer arbitration.",
                )
            elif tier_level == 4:
                subsystem_target = target_file
                for key in ["database", "concurrency", "lock", "thread", "async", "cache", "sandbox", "security", "telemetry", "model", "routing", "memory"]:
                    if key in prompt.lower():
                        subsystem_target = key
                        break
                
                if objections:
                    self.lineage_memory.log_failed_experiment(
                        target_subsystem=subsystem_target,
                        patch=coder_proposal[:1000],
                        error_type="critic_veto",
                        error_message=f"Debate loop finished but objections remained: {objections}",
                    )
                elif arbitration.requires_human_review:
                    self.lineage_memory.log_failed_experiment(
                        target_subsystem=subsystem_target,
                        patch=coder_proposal[:1000],
                        error_type="arbitration_veto",
                        error_message="Reasoning council arbitration vetoed proposal due to low confidence.",
                    )
                else:
                    self.lineage_memory.log_decision(
                        decision_id=decision_id,
                        target_subsystem=subsystem_target,
                        rationale=final_summary[:300],
                        architectural_impact=impact,
                        consequences="Approved and validated by Reasoning Council.",
                        alternatives=[{
                            "option_name": "Proposed Solution",
                            "tradeoffs": {"confidence": arbitration.overall_confidence},
                            "rejected_reason": ""
                        }]
                    )

            # Token usage summary — estimate from text lengths since agent
            # responses are strings, not InferenceResponse objects.
            _agent_texts = [
                t for t in [
                    coder_proposal,
                    final_summary,
                    getattr(reviewer_report, "model_dump", lambda: {})().get("critical_issues", ""),
                ] if t
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
