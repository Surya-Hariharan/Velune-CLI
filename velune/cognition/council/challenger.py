"""Challenger agent performing adversarial testing, edge case simulation, and assumption checks."""

from __future__ import annotations

import json
from typing import Dict, Any, List
import logging

from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider
from velune.cognition.council.base import BaseCouncilAgent

logger = logging.getLogger("velune.cognition.council.challenger")

CHALLENGER_SYSTEM_PROMPT = """You are the Adversarial Challenger for the Velune Reasoning Council.
Your role is to critically analyze proposals, challenge assumptions, and discover hidden failure modes.

Actively try to break the Coder's proposed plan or solution.
Identify:
1. Sneaky edge cases (e.g. empty files, massive inputs, network timeouts, OS-specific path quirks).
2. Unspoken assumptions the coder made that could fail in production.
3. Silent failure paths where errors might be swallowed.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "assumptions_challenged": [
    "Assumption challenged 1",
    "Assumption challenged 2"
  ],
  "failure_vectors": [
    "Potential failure path/edge case 1",
    "Potential failure path/edge case 2"
  ],
  "severity_rating": 0.0 to 1.0
}
"""


class ChallengerAgent(BaseCouncilAgent):
    """Challenger Agent identifying adversarial risks and hidden failures."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.CHALLENGER,
            model=model,
            provider=provider,
            system_prompt=CHALLENGER_SYSTEM_PROMPT,
        )

    async def challenge(self, task: str, proposal: str, context: str) -> Dict[str, Any]:
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

        raw_output = await self.deliberate(user_messages, temperature=0.6)

        # Clean markdown codeblocks if model didn't follow instructions
        cleaned = raw_output.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except Exception as e:
            logger.error("Failed to parse Challenger JSON output: %s", e)
            return {
                "assumptions_challenged": [],
                "failure_vectors": [f"Challenger was active but output format was unparseable: {raw_output}"],
                "severity_rating": 0.3,
            }
