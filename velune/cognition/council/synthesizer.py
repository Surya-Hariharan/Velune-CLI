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
        *,
        confidence: float | None = None,
        flags: list[str] | None = None,
        requires_human_review: bool | None = None,
        synthesis_instructions: str | None = None,
    ) -> str:
        """Assembles all council outputs into a premium walk-through response.

        Raises :class:`CouncilAgentError` if the model call times out, fails or
        returns nothing, so the caller can fall back instead of presenting a
        failure string as the answer.
        """
        logger.info("Synthesizer compiling council deliberation artifacts...")

        content = (
            f"ORIGINAL TASK: {task}\n\n"
            f"ARBITRATION WINNING CLAIMS:\n{winning_claims}\n\n"
            f"PROPOSED EXECUTION PLAN / CODE:\n{plan}\n\n"
            f"QUALITY AUDITS & CHALLENGER WARNINGS:\n{audit_reports}\n\n"
        )
        signals = []
        if confidence is not None:
            signals.append(f"Overall council confidence: {confidence:.2f}")
        if requires_human_review is not None:
            signals.append(f"Requires human review: {requires_human_review}")
        if flags:
            signals.append(f"Flags: {', '.join(flags)}")
        if synthesis_instructions:
            signals.append(f"Arbitration guidance:\n{synthesis_instructions}")
        if signals:
            content += "ARBITRATION SIGNALS (state residual risk accordingly):\n"
            content += "\n".join(signals) + "\n\n"
        content += f"WORKSPACE REPO CONTEXT:\n{context}"

        user_messages = [{"role": "user", "content": content}]

        return await self.deliberate(user_messages, temperature=0.3, strict=True)
