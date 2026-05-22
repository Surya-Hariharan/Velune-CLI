"""LangGraph-style orchestrator compiling council roles and executing deliberation flows."""

from __future__ import annotations

from typing import Dict, Any, List, Optional
import logging

from velune.models.specializations import ModelSpecializationMapper, CouncilRole
from velune.providers.registry import ProviderRegistry
from velune.cognition.council.planner import PlannerAgent
from velune.cognition.council.coder import CoderAgent
from velune.cognition.council.reviewer import ReviewerAgent
from velune.cognition.council.challenger import ChallengerAgent
from velune.cognition.council.synthesizer import SynthesizerAgent
from velune.cognition.arbitrator import CouncilArbitrator

logger = logging.getLogger("velune.cognition.orchestrator")


class CouncilOrchestrator:
    """Manages model mappings and runs the multi-agent Reasoning Council debate graph."""

    def __init__(
        self,
        provider_registry: ProviderRegistry,
        mapper: ModelSpecializationMapper,
        historical_accuracy: float = 0.85,
    ) -> None:
        self.provider_registry = provider_registry
        self.mapper = mapper
        self.arbitrator = CouncilArbitrator(historical_accuracy=historical_accuracy)

    async def execute_task(self, prompt: str, repo_context: str) -> Dict[str, Any]:
        """Orchestrate a complete council deliberation pass for a task prompt."""
        logger.info("Reasoning Council starting execution for goal: %s", prompt)

        # 1. Map specialized models to council roles
        roles = self.mapper.map_roles()
        
        # 2. Instantiate active council agent instances
        logger.info("Instantiating specialized council agents...")
        planner_model = roles[CouncilRole.PLANNER]
        coder_model = roles[CouncilRole.CODER]
        reviewer_model = roles[CouncilRole.REVIEWER]
        challenger_model = roles[CouncilRole.CHALLENGER]
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
        challenger = ChallengerAgent(
            model=challenger_model,
            provider=self.provider_registry.get_or_raise(challenger_model.provider_id),
        )
        synthesizer = SynthesizerAgent(
            model=synthesizer_model,
            provider=self.provider_registry.get_or_raise(synthesizer_model.provider_id),
        )

        # 3. Deliberation Phase A: Planner compiles DAG Execution Plan
        logger.info("[COUNCIL - PLANNER] Decomposing task into steps...")
        task_plan = await planner.generate_plan(prompt, repo_context)
        
        # 4. Deliberation Phase B: Coder generates solution proposal
        plan_desc = "\n".join([f"- {s.id}: {s.description}" for s in task_plan.steps])
        logger.info("[COUNCIL - CODER] Designing code implementation...")
        coder_proposal = await coder.write_code(
            prompt=prompt,
            current_code=repo_context,
            plan_context=plan_desc,
        )

        # 5. Deliberation Phase C: Reviewer static code quality audit
        logger.info("[COUNCIL - REVIEWER] Auditing solution syntax and safety...")
        reviewer_report = await reviewer.review(
            task=prompt,
            proposal=coder_proposal,
            context=repo_context,
        )

        # 6. Deliberation Phase D: Challenger adversarial analysis
        logger.info("[COUNCIL - CHALLENGER] Probing assumptions and edge cases...")
        challenger_report = await challenger.challenge(
            task=prompt,
            proposal=coder_proposal,
            context=repo_context,
        )

        # 7. Deliberation Phase E: Council Arbitration
        logger.info("[COUNCIL - ARBITRATION] Evaluating agent votes and contradictions...")
        arbitration = self.arbitrator.arbitrate(
            plan_steps=[s.description for s in task_plan.steps],
            coder_proposal=coder_proposal,
            reviewer_report=reviewer_report,
            challenger_report=challenger_report,
        )

        # 8. Deliberation Phase F: Final Response Synthesis
        logger.info("[COUNCIL - SYNTHESIS] Rendering walk-through response...")
        final_summary = await synthesizer.synthesize(
            task=prompt,
            winning_claims=arbitration.winning_claims,
            plan=coder_proposal,
            audit_reports=[reviewer_report, challenger_report],
            context=repo_context,
        )

        logger.info("Reasoning Council deliberation fully completed")
        return {
            "task_plan": task_plan,
            "coder_proposal": coder_proposal,
            "reviewer_report": reviewer_report,
            "challenger_report": challenger_report,
            "arbitration": arbitration.to_dict(),
            "final_summary": final_summary,
        }
