"""LangGraph-style orchestrator compiling council roles and executing deliberation flows."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

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
from velune.cognition.council.tiers import CouncilTier, classify_task_tier
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

    def get_state(self, run_id: str) -> Any | None:
        """Get the cached OrchestrationState by run_id."""
        return self._states.get(run_id)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Runs the Reasoning Council task execution and streams milestones."""
        import uuid
        from velune.orchestration.schemas import OrchestrationState, OrchestrationRequest, ExecutionStatus
        
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        
        yield f"[{run_id}] context reconstruction"
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
        except Exception as e:
            logger.warning("Could not gather repository snapshot: %s", e)

        yield f"[{run_id}] planning"
        yield f"[{run_id}] reasoning"
        yield f"[{run_id}] tool execution"
        yield f"[{run_id}] validation"
        yield f"[{run_id}] review"
        yield f"[{run_id}] finalize"

        try:
            result = await self._execute_task_inner(prompt, repo_context)
            final_summary = result.get("final_summary", "Execution completed successfully.")
            status = ExecutionStatus.COMPLETED
            error = None
        except Exception as e:
            logger.error("Reasoning Council execution failed: %s", e)
            final_summary = f"Execution failed: {e}"
            status = ExecutionStatus.FAILED
            error = str(e)

        request = OrchestrationRequest(prompt=prompt, workspace=".")
        state = OrchestrationState(
            run_id=run_id,
            request=request,
            status=status,
            output=final_summary,
            error=error,
            task_plan=result.get("task_plan") if 'result' in locals() and isinstance(result, dict) else None,
        )
        self._states[run_id] = state


    def _get_or_refresh_style_profile(self, target_file: str) -> dict[str, Any] | None:
        """Queries the style profile from database, or scans and caches it if missing/stale."""
        target_dir = os.path.dirname(target_file)
        if not target_dir:
            target_dir = "velune/core"

        # Check in the SQLite DB
        profile = self.lineage_memory.get_personality_style(target_dir)

        # If missing or older than 24 hours (86400 seconds), refresh it
        is_stale = False
        if profile:
            updated_at = profile.get("updated_at", 0.0)
            if time.time() - updated_at > 86400.0:
                is_stale = True

        if not profile or is_stale:
            try:
                # AST Scan
                from velune.cognition.personality import RepositoryPersonalityAgent
                agent = RepositoryPersonalityAgent()

                # Check if directory exists
                if os.path.exists(target_dir):
                    profile = agent.analyze_directory_style(target_dir)
                    # Cache in database
                    self.lineage_memory.save_personality_style(
                        subsystem=target_dir,
                        naming_conventions=profile["naming_conventions"],
                        type_hinting_strictness=profile["type_hinting_strictness"],
                        preferred_constructs=profile["preferred_constructs"],
                        class_vs_functional=profile["class_vs_functional"],
                        docstring_style=profile["docstring_style"],
                    )
            except Exception as e:
                logger.error("Failed to run RepositoryPersonalityAgent: %s", e)

        return profile

    def _is_structural_change(self, prompt: str, repo_context: str) -> bool:
        """
        Determines if a task requires structural modifications or is simple/trivial.
        Simple changes: UI tweaks, comment changes, formatting, small edits, basic lookups.
        Structural changes: class/method definitions, interface changes, multiple files, DB/concurrency related.
        """
        prompt_lower = prompt.lower()

        # 1. Structural indicators
        structural_keywords = [
            "redesign", "architect", "concurrency", "thread", "async", "lock", "database",
            "class", "interface", "refactor", "performance", "scalability", "security",
            "cohesion", "module", "coupling", "sandbox", "boundary", "lcom", "critic"
        ]

        for kw in structural_keywords:
            if kw in prompt_lower:
                return True

        # 2. Simple indicators
        simple_keywords = ["typo", "comment", "format", "rename variable", "ui text", "alignment", "simple tweak"]
        for kw in simple_keywords:
            if kw in prompt_lower:
                return False

        # 3. Length heuristic
        if len(prompt.split()) > 15:
            return True

        return False

    async def execute_task(
        self,
        prompt: str,
        repo_context: str,
        council_tier: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Orchestrate a complete council deliberation pass for a task prompt with wall-time limit."""
        # Calculate/classify tier first so that we know the tier in case of timeout
        roles = self.mapper.map_roles()
        coder_model = roles.get(CouncilRole.CODER)
        estimated_tps = 8.0  # conservative default
        if coder_model:
            profile = self.mapper.profiler.get_profile(coder_model.provider_id, coder_model.model_id)
            if profile and profile.tps > 0.0:
                estimated_tps = profile.tps

        if council_tier:
            try:
                tier = CouncilTier(council_tier.lower())
            except ValueError:
                tier = classify_task_tier(prompt, repo_context, available_tps=estimated_tps)
        else:
            tier = classify_task_tier(prompt, repo_context, available_tps=estimated_tps)

        # Dynamic low-resource optimization mode
        low_resource = (
            (self.config and self.config.execution.low_resource_mode) or
            os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")
        )
        
        if low_resource and tier == CouncilTier.FULL:
            tier = CouncilTier.STANDARD

        tier_str = tier.value

        try:
            return await asyncio.wait_for(
                self._execute_task_inner(
                    prompt=prompt,
                    repo_context=repo_context,
                    council_tier=council_tier,
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

    async def _execute_task_inner(
        self,
        prompt: str,
        repo_context: str,
        council_tier: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Internal execution body of the orchestrator task."""
        import uuid

        from velune.core.trace import TraceContext

        run_id = f"council-{uuid.uuid4().hex[:8]}"
        with TraceContext(run_id=run_id):
            # Estimate model speed from profiler
            roles = self.mapper.map_roles()
            coder_model = roles.get(CouncilRole.CODER)
            estimated_tps = 8.0  # conservative default
            if coder_model:
                profile = self.mapper.profiler.get_profile(coder_model.provider_id, coder_model.model_id)
                if profile and profile.tps > 0.0:
                    estimated_tps = profile.tps

            if council_tier:
                try:
                    tier = CouncilTier(council_tier.lower())
                except ValueError:
                    tier = classify_task_tier(prompt, repo_context, available_tps=estimated_tps)
            else:
                tier = classify_task_tier(prompt, repo_context, available_tps=estimated_tps)
                
            # Dynamic low-resource optimization mode
            low_resource = (
                (self.config and self.config.execution.low_resource_mode) or
                os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")
            )
            
            if low_resource and tier == CouncilTier.FULL:
                logger.info("[LOW-RESOURCE] Downgrading Council tier from FULL to STANDARD to optimize CPU usage.")
                tier = CouncilTier.STANDARD

            # Estimate cost (agent_count × estimated_seconds) before execution
            agent_counts = {
                CouncilTier.INSTANT: 1,
                CouncilTier.STANDARD: 4,
                CouncilTier.FULL: 10,
            }
            agent_count = agent_counts.get(tier, 4)
            estimated_seconds_per_call = 300.0 / estimated_tps
            estimated_cost_seconds = agent_count * estimated_seconds_per_call

            logger.info(
                "Council tier selected: %s for prompt: %s... (Estimated cost: %.1fs based on %d agents at %.1f TPS)",
                tier.value,
                prompt[:50],
                estimated_cost_seconds,
                agent_count,
                estimated_tps,
            )

            start_time = time.time()
            try:
                if tier == CouncilTier.INSTANT:
                    result = await self._execute_instant(prompt, repo_context, progress_callback)
                elif tier == CouncilTier.STANDARD:
                    result = await self._execute_standard(prompt, repo_context, progress_callback)
                else:
                    result = await self._execute_full(prompt, repo_context, progress_callback)
                return result
            finally:
                elapsed_time = time.time() - start_time
                logger.info("Executed %s tier in %.2fs", tier.value, elapsed_time)

    async def _execute_instant(self, prompt: str, repo_context: str, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
        """FAST PATH INSTANT: Coder only, no debate, no review, but still runs CognitiveFirewall scan on repo context."""
        logger.info("[COUNCIL - INSTANT] Executing Instant single-agent Coder path...")

        # INSTANT tier must still run CognitiveFirewall scan on repo context
        from velune.cognition.firewall import CognitiveFirewall
        firewall = CognitiveFirewall()

        # Scan repo context for security issues
        if not firewall.scan_file_for_injection("workspace_context", repo_context)["is_safe"]:
            logger.error("Security: prompt injection detected in workspace context during Instant execution")
            raise ValueError("Security: Potential prompt injection detected in workspace context")

        target_file = "velune/core/main.py"
        py_files = re.findall(r"[\w\/\.\-]+\.py", prompt)
        if py_files:
            target_file = py_files[0]

        style_profile = self._get_or_refresh_style_profile(target_file)

        roles = self.mapper.map_roles()
        coder_model = roles[CouncilRole.CODER]
        coder = CoderAgent(
            model=coder_model,
            provider=self.provider_registry.get_or_raise(coder_model.provider_id),
        )

        logger.info("Council Phase: Coder")
        if progress_callback:
            progress_callback("[Coder] Designing code implementation...")

        coder_proposal = await coder.write_code(
            prompt=prompt,
            current_code=repo_context,
            plan_context="Direct implementation (Instant path chosen).",
            style_profile=style_profile,
        )

        return {
            "tier": "instant",
            "task_plan": None,
            "coder_proposal": coder_proposal,
            "reviewer_report": None,
            "challenger_report": None,
            "arbitration": {"overall_confidence": 0.85, "requires_human_review": False},
            "final_summary": coder_proposal,
        }

    async def _execute_standard(
        self,
        prompt: str,
        repo_context: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """STANDARD PATH: Planner + Coder + Reviewer + Synthesizer (No Challenger, no specialized critics, Max 1 debate turn)"""
        logger.info("[COUNCIL - STANDARD] Executing Standard Coder + Reviewer path...")

        target_file = "velune/core/main.py"
        py_files = re.findall(r"[\w\/\.\-]+\.py", prompt)
        if py_files:
            target_file = py_files[0]

        style_profile = self._get_or_refresh_style_profile(target_file)

        roles = self.mapper.map_roles()
        planner_model = roles[CouncilRole.PLANNER]
        coder_model = roles[CouncilRole.CODER]
        reviewer_model = roles[CouncilRole.REVIEWER]
        synthesizer_model = roles[CouncilRole.SYNTHESIZER]

        planner = PlannerAgent(
            model=planner_model,
            provider=self.provider_registry.get_or_raise(planner_model.provider_id),
        )
        coder = CoderAgent(
            model=coder_model,
            provider=self.provider_registry.get_or_raise(coder_model.provider_id),
        )
        reviewer = ReviewerAgent(
            model=reviewer_model,
            provider=self.provider_registry.get_or_raise(reviewer_model.provider_id),
        )
        synthesizer = SynthesizerAgent(
            model=synthesizer_model,
            provider=self.provider_registry.get_or_raise(synthesizer_model.provider_id),
        )

        # 1. Generate plan
        logger.info("Council Phase: Planner")
        if progress_callback:
            progress_callback("[Planner] Decomposing task...")
        task_plan = await planner.generate_plan(prompt, repo_context)
        plan_desc = "\n".join([f"- {s.id}: {s.description}" for s in task_plan.steps])

        # 2. Generate code
        logger.info("Council Phase: Coder")
        if progress_callback:
            progress_callback("[Coder] Designing code implementation...")
        coder_proposal = await coder.write_code(
            prompt=prompt,
            current_code=repo_context,
            plan_context=plan_desc,
            style_profile=style_profile,
        )

        # 3. Review code
        logger.info("Council Phase: Reviewer")
        if progress_callback:
            progress_callback("[Reviewer] Reviewing code proposal...")
        reviewer_report = await reviewer.review(task=prompt, proposal=coder_proposal, context=repo_context)

        # Max 1 debate turn
        objections = []
        if not reviewer_report.passed:
            objections.append(f"Reviewer: {reviewer_report.critical_issues}")

        low_resource = (
            (self.config and self.config.execution.low_resource_mode) or
            os.environ.get("VELUNE_LOW_RESOURCE", "").lower() in ("true", "1", "yes")
        )

        if objections and not low_resource:
            logger.info("[COUNCIL - STANDARD] Objection detected. Running 1 refinement turn...")
            objections_text = "\n".join([f"- {obj}" for obj in objections])
            refine_prompt = (
                f"The Reviewer has raised the following objections to your previous proposal:\n"
                f"{objections_text}\n\n"
                f"Please rewrite and refine the proposed code to resolve these objections."
            )
            coder_proposal = await coder.write_code(
                prompt=prompt,
                current_code=repo_context,
                plan_context=f"Standard Refinement Turn:\n{refine_prompt}",
                style_profile=style_profile,
            )
            # Re-run reviewer once
            reviewer_report = await reviewer.review(task=prompt, proposal=coder_proposal, context=repo_context)

        # 4. Arbitration
        logger.info("Council Phase: Arbitration")
        if progress_callback:
            progress_callback("[Arbitration] Arbitrating proposal...")
        arbitration = self.arbitrator.arbitrate(
            plan_steps=[s.description for s in task_plan.steps],
            coder_proposal=coder_proposal,
            reviewer_report=reviewer_report,
            challenger_report=None,  # No Challenger
            scalability_report=None,
            security_report=None,
            performance_report=None,
            maintainability_report=None,
        )

        # 5. Synthesize Walkthrough
        logger.info("Council Phase: Synthesis")
        if progress_callback:
            progress_callback("[Synthesis] Synthesizing final summary...")
        final_summary = await synthesizer.synthesize(
            task=prompt,
            winning_claims=arbitration.winning_claims,
            plan=coder_proposal,
            audit_reports=[reviewer_report.model_dump()],
            context=repo_context,
        )

        # Log successful decision to DLS
        decision_id = f"DEC-{int(time.time())}"
        self.lineage_memory.log_decision(
            decision_id=decision_id,
            target_subsystem="standard_path",
            rationale=final_summary[:300],
            architectural_impact=0.3,
            consequences="Standard execution completed with single-reviewer arbitration.",
        )

        return {
            "tier": "standard",
            "task_plan": task_plan,
            "coder_proposal": coder_proposal,
            "reviewer_report": reviewer_report,
            "challenger_report": None,
            "arbitration": arbitration.to_dict(),
            "final_summary": final_summary,
        }

    async def _execute_full(
        self,
        prompt: str,
        repo_context: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Orchestrate a complete council deliberation pass for a task prompt (FULL tier)."""
        logger.info("Reasoning Council starting execution in FULL tier for goal: %s", prompt)

        # Resolve target file and directory for style extraction
        target_file = "velune/core/main.py"  # Default fallback
        py_files = re.findall(r"[\w\/\.\-]+\.py", prompt)
        if py_files:
            target_file = py_files[0]

        style_profile = self._get_or_refresh_style_profile(target_file)

        # 1. Map specialized models to council roles
        roles = self.mapper.map_roles()

        # 2. Instantiate active council agent instances
        logger.info("Instantiating specialized council agents...")
        planner_model = roles[CouncilRole.PLANNER]
        coder_model = roles[CouncilRole.CODER]
        reviewer_model = roles[CouncilRole.REVIEWER]
        challenger_model = roles[CouncilRole.CHALLENGER]
        synthesizer_model = roles[CouncilRole.SYNTHESIZER]

        coder = CoderAgent(
            model=coder_model,
            provider=self.provider_registry.get_or_raise(coder_model.provider_id),
        )
        synthesizer = SynthesizerAgent(
            model=synthesizer_model,
            provider=self.provider_registry.get_or_raise(synthesizer_model.provider_id),
        )

        # --- STRUCTURAL CHANGE PATH ---
        logger.info("[COUNCIL - COMPLEXITY] Launching Architecture Cognition Agent & Multi-Critique Council...")

        # ACA Analysis
        logger.info("[COUNCIL - ACA] Executing Architecture Cognition Agent audit on: %s", target_file)
        architectural_context = ""
        if "class " in repo_context or "def " in repo_context:
            self.architecture_agent.audit_architecture(target_file, repo_context)

            # Retrieve persistent ledger items to enrich reasoning
            debt_items = self.architecture_agent.ledger.get_items()
            if debt_items:
                architectural_context += "\n--- KNOWN ARCHITECTURAL DEBT & VIOLATIONS ---\n"
                for item in debt_items:
                    architectural_context += f"- [{item['category'].upper()}] in '{item['file_path']}': {item['description']} (Severity: {item['severity']})\n"
                architectural_context += "Please ensure the proposed code fixes or avoids increasing this technical debt.\n"

                # Check for active layering violations to append blocking alarm warning
                layering_violations = [item for item in debt_items if item["category"] == "layering"]
                if layering_violations:
                    architectural_context += "\n==================================================\n"
                    architectural_context += "!!! ARCHITECTURE DRIFT ALARM (ADA) ACTIVE BLOCK !!!\n"
                    architectural_context += "The following layering boundary violations MUST BE RESOLVED IMMEDIATELY:\n"
                    for item in layering_violations:
                        architectural_context += f"- BLOCKING DRIFT: {item['description']} (File: {item['file_path']})\n"
                    architectural_context += "You MUST plan to fix these import boundary violations in this execution pass.\n"
                    architectural_context += "==================================================\n\n"

        # Query past architectural decisions and failed experiment warnings
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

        # SECURE WORKSPACE DATA VIA XML WRAPPING
        architectural_context = self.firewall.wrap_workspace_content(
            "architectural_debt_ledger", architectural_context
        )
        continuity_context = self.firewall.wrap_workspace_content(
            "continuity_warnings", continuity_context
        )

        enriched_repo_context = repo_context + architectural_context + continuity_context

        # Instantiate Planner, Reviewer, Challenger, and specialized critics
        planner = PlannerAgent(
            model=planner_model,
            provider=self.provider_registry.get_or_raise(planner_model.provider_id),
        )
        reviewer = ReviewerAgent(
            model=reviewer_model,
            provider=self.provider_registry.get_or_raise(reviewer_model.provider_id),
        )
        challenger = ChallengerAgent(
            model=challenger_model,
            provider=self.provider_registry.get_or_raise(challenger_model.provider_id),
        )

        scalability_critic = ScalabilityCritic(
            model=challenger_model,
            provider=self.provider_registry.get_or_raise(challenger_model.provider_id),
        )
        security_critic = SecurityCritic(
            model=reviewer_model,
            provider=self.provider_registry.get_or_raise(reviewer_model.provider_id),
        )
        performance_critic = PerformanceCritic(
            model=reviewer_model,
            provider=self.provider_registry.get_or_raise(reviewer_model.provider_id),
        )
        maintainability_critic = MaintainabilityCritic(
            model=reviewer_model,
            provider=self.provider_registry.get_or_raise(reviewer_model.provider_id),
        )

        # 4. Deliberation Phase A: Planner compiles DAG Execution Plan
        logger.info("Council Phase: Planner")
        if progress_callback:
            progress_callback("[Planner] Decomposing task...")
        logger.info("[COUNCIL - PLANNER] Decomposing task into steps...")
        task_plan = await planner.generate_plan(prompt, enriched_repo_context)

        # 5. Deliberation Phase B: Coder generates solution proposal
        logger.info("Council Phase: Coder")
        if progress_callback:
            progress_callback("[Coder] Designing code implementation...")
        plan_desc = "\n".join([f"- {s.id}: {s.description}" for s in task_plan.steps])
        logger.info("[COUNCIL - CODER] Designing code implementation...")
        coder_proposal = await coder.write_code(
            prompt=prompt,
            current_code=enriched_repo_context,
            plan_context=plan_desc,
            style_profile=style_profile,
        )

        # 6. Deliberation Phase C & D: Concurrent Review and Parallel Critics Council
        logger.info("Council Phase: Reviewer+Challenger")
        if progress_callback:
            progress_callback("[Reviewer+Challenger] Running parallel review and critique...")
        logger.info("[COUNCIL - PARALLEL] Launching parallel adversarial review & 4 specialized critics...")

        reviewer_task = reviewer.review(task=prompt, proposal=coder_proposal, context=repo_context)
        challenger_task = challenger.challenge(task=prompt, proposal=coder_proposal, context=repo_context)
        scalability_task = scalability_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context)
        security_task = security_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context)
        performance_task = performance_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context)
        maintainability_task = maintainability_critic.critique(task=prompt, proposal=coder_proposal, context=repo_context)

        (
            reviewer_report,
            challenger_report,
            scalability_report,
            security_report,
            performance_report,
            maintainability_report,
        ) = await asyncio.gather(
            reviewer_task,
            challenger_task,
            scalability_task,
            security_task,
            performance_task,
            maintainability_task,
        )
        logger.info("[COUNCIL - PARALLEL] Parallel reviews successfully gathered.")

        # 7. Contradiction-Driven Arbitration Multi-Agent Debate Loop
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
            objections.append(f"Challenger (Severity: {challenger_report.severity_rating}): {challenger_report.failure_vectors}")

        initial_objection_count = len(objections)
        converged = (initial_objection_count == 0)
        turns_required = 0
        debate_start_time = time.time()

        all_critic_reports = {
            "security": security_report,
            "scalability": scalability_report,
            "challenger": challenger_report,
        }

        # Calculate debate turns based on complexity and scores, but capped at 3
        max_debate_turns = calculate_max_debate_turns(
            initial_objections=objections,
            critic_reports=all_critic_reports,
            task_complexity="structural",
        )
        max_debate_turns = min(3, max_debate_turns)
        logger.info("Debate configured for %d max turns (objections: %d)",
                    max_debate_turns, len(objections))

        refined_proposal = coder_proposal

        if objections and max_debate_turns > 0:
            logger.info("[COUNCIL - DEBATE] Objections detected. Initiating Contradiction-Driven Arbitration Debate Loop...")

            async def _run_debate_loop() -> None:
                nonlocal coder_proposal, refined_proposal, reviewer_report, scalability_report, security_report, performance_report, maintainability_report, objections, converged, turns_required

                debate_turn = 1
                refined_proposal = coder_proposal

                while debate_turn <= max_debate_turns:
                    logger.info("[COUNCIL - DEBATE] Debate Loop Turn %d/%d", debate_turn, max_debate_turns)
                    logger.info("Council Phase: Debate turn %d", debate_turn)
                    if progress_callback:
                        progress_callback(f"[Debate] Running debate turn {debate_turn}...")
                    turns_required = debate_turn

                    objections_text = "\n".join([f"- {obj}" for obj in objections])
                    refine_prompt = (
                        f"The Reasoning Council has raised the following objections to your previous proposal:\n"
                        f"{objections_text}\n\n"
                        f"Please rewrite and refine the proposed code to resolve ALL of these objections completely while satisfying the original task."
                    )

                    logger.info("[COUNCIL - DEBATE] Coder refining proposal...")
                    refined_proposal = await coder.write_code(
                        prompt=prompt,
                        current_code=repo_context,
                        plan_context=f"Debate Refinement (Turn {debate_turn}):\n{refine_prompt}",
                        style_profile=style_profile,
                    )

                    logger.info("[COUNCIL - DEBATE] Re-running objecting critics on refined proposal...")
                    re_tasks: list[Any] = []
                    re_critics: list[str] = []

                    if not reviewer_report.passed:
                        re_tasks.append(reviewer.review(task=prompt, proposal=refined_proposal, context=repo_context))
                        re_critics.append("reviewer")
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
                        re_results = await asyncio.gather(*re_tasks)

                        # Update all reports first to preserve converged state if we break early
                        for name, res in zip(re_critics, re_results):
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

                        # convergence detection with score > 0.8
                        all_passed_with_high_score = True
                        for name, res in zip(re_critics, re_results):
                            score = res.confidence_rating if name == "reviewer" else getattr(res, "score", 1.0)
                            if not res.passed or score <= 0.8:
                                all_passed_with_high_score = False
                                break

                        if all_passed_with_high_score:
                            logger.info("Full convergence achieved on turn %d", debate_turn)
                            coder_proposal = refined_proposal
                            objections = []
                            converged = True
                            break

                        new_objections = []
                        for name, res in zip(re_critics, re_results):
                            if name == "reviewer":
                                if not res.passed:
                                    new_objections.append(f"Reviewer: {res.critical_issues}")
                            elif name == "scalability":
                                if not res.passed:
                                    new_objections.append(f"Scalability Critic: {res.issues}")
                            elif name == "security":
                                if not res.passed:
                                    new_objections.append(f"Security Critic: {res.issues}")
                            elif name == "performance":
                                if not res.passed:
                                    new_objections.append(f"Performance Critic: {res.issues}")
                            elif name == "maintainability":
                                if not res.passed:
                                    new_objections.append(f"Maintainability Critic: {res.issues}")
                        objections = new_objections

                    else:
                        objections = []

                    if not objections:
                        logger.info("Debate converged after %d turns", debate_turn)
                        logger.info("[COUNCIL - DEBATE] Debate loop converged! All objections resolved.")
                        coder_proposal = refined_proposal
                        converged = True
                        break

                    debate_turn += 1

                if objections:
                    logger.warning("[COUNCIL - DEBATE] Debate loop finished but objections remain: %s", objections)
                    coder_proposal = refined_proposal
                    converged = False

            MAX_DEBATE_WALL_TIME = 300.0  # 5 minutes hard cap for entire debate
            try:
                await asyncio.wait_for(_run_debate_loop(), timeout=MAX_DEBATE_WALL_TIME)
            except TimeoutError:
                logger.warning("Debate loop hit wall time limit, proceeding with best proposal")
                converged = False

        if initial_objection_count > 0:
            time_to_converge_ms = int((time.time() - debate_start_time) * 1000)
            self.analytics.record_debate_outcome(
                turns_required=turns_required,
                initial_objection_count=initial_objection_count,
                final_objection_count=len(objections),
                converged=converged,
                time_to_converge_ms=time_to_converge_ms,
            )

        # 8. Council Arbitration and Confidence Fusion
        logger.info("Council Phase: Arbitration")
        if progress_callback:
            progress_callback("[Arbitration] Arbitrating proposal...")
        logger.info("[COUNCIL - ARBITRATION] Evaluating agent votes and contradictions...")
        arbitration = self.arbitrator.arbitrate(
            plan_steps=[s.description for s in task_plan.steps],
            coder_proposal=coder_proposal,
            reviewer_report=reviewer_report,
            challenger_report=challenger_report,
            scalability_report=scalability_report,
            security_report=security_report,
            performance_report=performance_report,
            maintainability_report=maintainability_report,
        )

        fusion_score = arbitration.overall_confidence
        logger.info(
            "[COUNCIL - FUSION] Reasoning Confidence Fusion Score: %.3f (Objections: %d)",
            fusion_score,
            len(objections),
        )

        # 9. Final Response Synthesis
        logger.info("Council Phase: Synthesis")
        if progress_callback:
            progress_callback("[Synthesis] Synthesizing final summary...")
        logger.info("[COUNCIL - SYNTHESIS] Rendering walk-through response...")
        final_summary = await synthesizer.synthesize(
            task=prompt,
            winning_claims=arbitration.winning_claims,
            plan=coder_proposal,
            audit_reports=[
                reviewer_report.model_dump(),
                challenger_report.model_dump(),
                scalability_report.model_dump(),
                security_report.model_dump(),
                performance_report.model_dump(),
                maintainability_report.model_dump(),
            ],
            context=repo_context,
        )

        # Determine subsystem keyword target
        subsystem_target = "general"
        py_files = re.findall(r"[\w\/\.\-]+\.py", prompt)
        if py_files:
            subsystem_target = py_files[0]
        else:
            # Match prompt keyword
            for key in ["database", "concurrency", "lock", "thread", "async", "cache", "sandbox", "security", "telemetry", "model", "routing", "memory"]:
                if key in prompt.lower():
                    subsystem_target = key
                    break

        if objections:
            # Log the unresolved objections as a failed experiment in the FEL
            self.lineage_memory.log_failed_experiment(
                target_subsystem=subsystem_target,
                patch=coder_proposal[:1000],
                error_type="critic_veto",
                error_message=f"Debate loop finished but objections remained: {objections}",
            )
        elif arbitration.requires_human_review:
            # Log the vetoed/failed experiment in the FEL
            self.lineage_memory.log_failed_experiment(
                target_subsystem=subsystem_target,
                patch=coder_proposal[:1000],
                error_type="arbitration_veto",
                error_message="Reasoning council arbitration vetoed proposal due to low confidence.",
            )
        else:
            # Log the successful decision in the DLS
            decision_id = f"DEC-{int(time.time())}"
            self.lineage_memory.log_decision(
                decision_id=decision_id,
                target_subsystem=subsystem_target,
                rationale=final_summary[:300],
                architectural_impact=0.7,
                consequences="Approved and validated by Reasoning Council.",
                alternatives=[
                    {
                        "option_name": "Proposed Solution",
                        "tradeoffs": {"confidence": arbitration.overall_confidence},
                        "rejected_reason": ""
                    }
                ]
            )

            pass

        logger.info("Reasoning Council deliberation fully completed")
        return {
            "tier": "full",
            "task_plan": task_plan,
            "coder_proposal": coder_proposal,
            "reviewer_report": reviewer_report,
            "challenger_report": challenger_report,
            "scalability_report": scalability_report,
            "security_report": security_report,
            "performance_report": performance_report,
            "maintainability_report": maintainability_report,
            "arbitration": arbitration.to_dict(),
            "final_summary": final_summary,
        }


