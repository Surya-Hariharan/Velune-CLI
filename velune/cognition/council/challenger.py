"""Challenger agent performing adversarial testing, edge case simulation, and assumption checks."""

from __future__ import annotations

import logging

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import ChallengerMessage
from velune.cognition.prompts import COUNCIL_CHALLENGER, get_prompt
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council.challenger")

CHALLENGER_SYSTEM_PROMPT = get_prompt(COUNCIL_CHALLENGER)


class ChallengerAgent(BaseCouncilAgent):
    """Challenger Agent identifying adversarial risks and hidden failures."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.CHALLENGER,
            model=model,
            provider=provider,
            system_prompt=CHALLENGER_SYSTEM_PROMPT,
        )

    async def challenge(self, task: str, proposal: str, context: str) -> ChallengerMessage:
        """Perform adversarial analysis on planned modifications."""
        logger.info("Challenger analyzing proposed changes for vulnerabilities...")

        user_messages = [
            {
                "role": "user",
                "content": (
                    f"TASK TO AUDIT: {task}\n\n"
                    f"PROPOSED CHANGE: \n{proposal}\n\n"
                    f"WORKSPACE CONTEXT AND ORIGINAL DATA:\n{context}"
                ),
            }
        ]

        result = await self.typed_deliberate(user_messages, ChallengerMessage, temperature=0.6)
        if result.parse_error:
            logger.warning(
                "Challenger parse failed, using degraded default: %s", result.parse_error
            )
            result.failure_vectors.append(
                f"Challenger was active but output format was unparseable: {result.parse_error}"
            )
        return result
