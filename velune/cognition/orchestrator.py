"""LangGraph-style orchestrator compiling council roles and executing deliberation flows."""

from __future__ import annotations

import asyncio
import logging
import re
import os
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

from velune.memory.tiers.lineage import LineageMemoryTier

from velune.models.specializations import ModelSpecializationMapper, CouncilRole
from velune.providers.registry import ProviderRegistry
from velune.cognition.council.planner import PlannerAgent
from velune.cognition.council.coder import CoderAgent
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.synthesizer import SynthesizerAgent
from velune.cognition.arbitrator import CouncilArbitrator
from velune.cognition.architecture import ArchitectureCognitionAgent
from velune.cognition.tradeoff import TradeoffEvaluationMatrix
from velune.cognition.evolution import EvolutionTimelineReporter
from velune.cognition.council.critics import (
    ScalabilityCritic,
    SecurityCritic,
    PerformanceCritic,
    MaintainabilityCritic,
)
from velune.cognition.council.debate import calculate_max_debate_turns
from velune.telemetry.cognition import CognitivePerformanceAnalytics


logger = logging.getLogger("velune.cognition.orchestrator")


class CouncilOrchestrator:
    """Manages model mappings and runs the multi-agent Reasoning Council debate graph."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        mapper: ModelSpecializationMapper,
        historical_accuracy: float = 0.85,
        lineage_db_path: Path | None = None,
        analytics: CognitivePerformanceAnalytics | None = None,
    ) -> None:
        self.provider_registry = provider_registry
        self.mapper = mapper
        self.arbitrator = CouncilArbitrator(historical_accuracy=historical_accuracy)
        self.architecture_agent = ArchitectureCognitionAgent(workspace_root=None, ledger=None)
        
        db_path = lineage_db_path or Path(".velune") / "cognition" / "decision_lineage.db"
        self.lineage_memory = LineageMemoryTier(db_path)
        self.evolution_reporter = EvolutionTimelineReporter(self.lineage_memory)
        self.analytics = analytics or CognitivePerformanceAnalytics()


    def _get_or_refresh_style_profile(self, target_file: str) -> Optional[Dict[str, Any]]:
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

    async def execute_task(self, prompt: str, repo_context: str) -> Dict[str, Any]:
        """Orchestrate a complete council deliberation pass for a task prompt."""
        logger.info("Reasoning Council starting execution for goal: %s", prompt)

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

        # 3. Assess Complexity and route to Fast-Path if simple
        if not self._is_structural_change(prompt, repo_context):
            logger.info("[COUNCIL - COMPLEXITY] Task classified as simple. Executing Fast-Path Single-Agent execution...")
            coder_proposal = await coder.write_code(
                prompt=prompt,
                current_code=repo_context,
                plan_context="Fast-path bypass plan: Execute direct change.",
                style_profile=style_profile,
            )
            
            final_summary = await synthesizer.synthesize(
                task=prompt,
                winning_claims=["Direct execution path chosen for simple change."],
                plan=coder_proposal,
                audit_reports=[],
                context=repo_context,
            )
            
            fast_path_arbitration = {
                "requires_human_review": False,
                "winning_claims": ["Direct execution path chosen for simple change."],
                "overall_confidence": 0.95,
                "flags": ["FAST_PATH"],
                "synthesis_instructions": "Direct approval.",
            }
            
            # Log the fast-path decision
            decision_id = f"DEC-{int(time.time())}"
            self.lineage_memory.log_decision(
                decision_id=decision_id,
                target_subsystem="fast_path",
                rationale=final_summary[:300],
                architectural_impact=0.1,
                consequences="Fast-path single-agent execution approved direct change.",
            )
            
            return {
                "task_plan": None,
                "coder_proposal": coder_proposal,
                "reviewer_report": None,
                "challenger_report": None,
                "arbitration": fast_path_arbitration,
                "final_summary": final_summary,
            }

        # --- STRUCTURAL CHANGE PATH ---
        logger.info("[COUNCIL - COMPLEXITY] Task classified as structural. Launching Architecture Cognition Agent & Multi-Critique Council...")
        
        # ACA Analysis
        target_file = "velune/core/main.py"  # Default fallback
        py_files = re.findall(r"[\w\/\.\-]+\.py", prompt)
        if py_files:
            target_file = py_files[0]
            
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
            continuity_context += "--------------------------------------\n"

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
        logger.info("[COUNCIL - PLANNER] Decomposing task into steps...")
        task_plan = await planner.generate_plan(prompt, enriched_repo_context)
        
        # 5. Deliberation Phase B: Coder generates solution proposal
        plan_desc = "\n".join([f"- {s.id}: {s.description}" for s in task_plan.steps])
        logger.info("[COUNCIL - CODER] Designing code implementation...")
        coder_proposal = await coder.write_code(
            prompt=prompt,
            current_code=enriched_repo_context,
            plan_context=plan_desc,
            style_profile=style_profile,
        )

        # 6. Deliberation Phase C & D: Concurrent Review and Parallel Critics Council
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
        max_debate_turns = calculate_max_debate_turns(
            initial_objections=objections,
            critic_reports=all_critic_reports,
            task_complexity="structural",
        )
        logger.info("Debate configured for %d max turns (objections: %d)", 
                    max_debate_turns, len(objections))

        if objections and max_debate_turns > 0:
            logger.info("[COUNCIL - DEBATE] Objections detected. Initiating Contradiction-Driven Arbitration Debate Loop...")
            
            debate_turn = 1
            refined_proposal = coder_proposal
            
            while debate_turn <= max_debate_turns:
                logger.info("[COUNCIL - DEBATE] Debate Loop Turn %d/%d", debate_turn, max_debate_turns)
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
                re_tasks = []
                re_critics = []
                
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
                architectural_impact=0.7 if self._is_structural_change(prompt, repo_context) else 0.2,
                consequences="Approved and validated by Reasoning Council.",
                alternatives=[
                    {
                        "option_name": "Proposed Solution",
                        "tradeoffs": {"confidence": arbitration.overall_confidence},
                        "rejected_reason": ""
                    }
                ]
            )

            # ── Phase 5: TEM evaluation of winning proposal vs. fast-path alternatives ──
            try:
                tem = TradeoffEvaluationMatrix(
                    task_id=decision_id,
                    lineage_memory=self.lineage_memory,
                )
                tem.add_option(
                    name="Council Proposal (Multi-Agent)",
                    metrics={
                        "performance": min(1.0, arbitration.overall_confidence),
                        "maintainability": 1.0 - (len(objections) / 6.0),
                        "safety": 0.85 if not objections else 0.60,
                        "scalability": 0.75,
                        "simplicity": 0.65,
                    },
                    notes="Multi-agent council deliberation with critic review.",
                )
                tem.add_option(
                    name="Fast-Path Alternative (Single-Agent)",
                    metrics={
                        "performance": 0.70,
                        "maintainability": 0.60,
                        "safety": 0.55,
                        "scalability": 0.50,
                        "simplicity": 0.90,
                    },
                    notes="Direct single-agent execution without debate or review.",
                )
                tem_winner = tem.select_optimal()
                logger.info(
                    "[COUNCIL - TEM] Trade-off matrix selected: '%s' (score=%.4f)",
                    tem_winner.name,
                    tem_winner.weighted_score,
                )
            except Exception as tem_err:
                logger.warning("TEM evaluation skipped: %s", tem_err)

            # ── Phase 5: Evolution timeline snapshot ──
            try:
                if py_files:
                    snap_dir = os.path.dirname(py_files[0])
                else:
                    snap_dir = subsystem_target

                shi_score = self.architecture_agent.calculate_shi(snap_dir) if os.path.exists(snap_dir) else 0.0
                coupling = self.architecture_agent.calculate_coupling_ratio(snap_dir) if os.path.exists(snap_dir) else 0.0
                debt_count = len(self.architecture_agent.ledger.get_items())

                self.evolution_reporter.snapshot_current_health(
                    subsystem=subsystem_target,
                    lcom_average=max(0.0, round(1.0 - shi_score, 3)),
                    coupling_ratio=coupling,
                    debt_items_count=debt_count,
                    milestone=None,
                    rationale_summary=final_summary[:300],
                )
                logger.info(
                    "[COUNCIL - EVOLUTION] Architecture snapshot logged for '%s'.",
                    subsystem_target,
                )
            except Exception as evo_err:
                logger.warning("Evolution snapshot skipped: %s", evo_err)

        logger.info("Reasoning Council deliberation fully completed")
        return {
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
