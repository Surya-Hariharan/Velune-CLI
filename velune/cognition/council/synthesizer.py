"""Synthesizer agent compiling deliberation results into a unified final proposal."""

from __future__ import annotations

import logging
from typing import Any

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.prompts import COUNCIL_SYNTHESIZER, get_prompt
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council.synthesizer")

SYNTHESIZER_SYSTEM_PROMPT = get_prompt(COUNCIL_SYNTHESIZER)


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
        winning_claims: list[str],
        plan: str,
        audit_reports: list[dict[str, Any]],
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
