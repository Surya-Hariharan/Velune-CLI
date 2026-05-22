"""Synthesizer agent compiling deliberation results into a unified final proposal."""

from __future__ import annotations

from typing import Dict, Any, List
import logging

from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider
from velune.cognition.council.base import BaseCouncilAgent

logger = logging.getLogger("velune.cognition.council.synthesizer")

SYNTHESIZER_SYSTEM_PROMPT = """You are the Lead Synthesizer for the Velune Reasoning Council.
Your role is to compile all agent findings, reviews, and challenges into a single, cohesive, premium final response.

Examine the:
1. Original user task.
2. Winning claims and decisions.
3. Reviewer quality checks and Challenger failure warnings.
4. Proposed plan and codebase modifications.

Produce a clear, detailed, and highly professional markdown response:
- Acknowledge any critical risks highlighted by the Reviewer or Challenger.
- Summarize the structural solution precisely.
- Output the finalized code changes, patches, or execution steps clearly.
- Provide a clear explanation of how the changes work and how to run or test them.
"""


class SynthesizerAgent(BaseCouncilAgent):
    """Synthesizer Agent assembling the final execution outputs."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.SYNTHESIZER,
            model=model,
            provider=provider,
            system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
        )

    async def synthesize(
        self,
        task: str,
        winning_claims: List[str],
        plan: str,
        audit_reports: List[Dict[str, Any]],
        context: str,
    ) -> str:
        """Assembles all council outputs into a premium walk-through response."""
        logger.info("Synthesizer compiling council deliberation artifacts...")

        user_messages = [
            {
                "role": "user",
                "content": (
                    f"ORIGINAL TASK: {task}\n\n"
                    f"ARBITRATION WINNING CLAIMS:\n{winning_claims}\n\n"
                    f"PROPOSED EXECUTION PLAN / CODE:\n{plan}\n\n"
                    f"QUALITY AUDITS & CHALLENGER WARNINGS:\n{audit_reports}\n\n"
                    f"WORKSPACE REPO CONTEXT:\n{context}"
                ),
            }
        ]

        return await self.deliberate(user_messages, temperature=0.3)
