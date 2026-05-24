"""Configurable Critic Agent class for the Reasoning Council."""

from __future__ import annotations

import logging

from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider
from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import CriticMessage
from velune.cognition.council.critic_configs import CriticConfig

logger = logging.getLogger("velune.cognition.council.critic_agent")


class CriticAgent(BaseCouncilAgent):
    """Configurable critic agent. Replace all specific critic classes with this."""

    def __init__(self, config: CriticConfig, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=config.council_role,
            model=model,
            provider=provider,
            system_prompt=config.system_prompt,
        )
        self.config = config

    async def critique(self, task: str, proposal: str, context: str) -> CriticMessage:
        user_messages = [
            {
                "role": "user",
                "content": f"TASK: {task}\n\nPROPOSAL:\n{proposal}\n\nCONTEXT:\n{context}",
            }
        ]
        result = await self.typed_deliberate(user_messages, CriticMessage, temperature=self.config.temperature)
        if result.parse_error:
            logger.warning("%sCritic parse failed: %s", self.config.name, result.parse_error)
            result.rationale = f"Critic output was unparseable: {result.parse_error}"
        return result
