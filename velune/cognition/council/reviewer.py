"""Reviewer agent auditing planned steps and generated code for bugs, styles, and regressions."""

from __future__ import annotations

import logging

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import ReviewerMessage
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council.reviewer")

REVIEWER_SYSTEM_PROMPT = """You are the Senior Code Reviewer for the Velune Reasoning Council.
Your role is to perform quality, safety, style, and regression audits on proposed plans and code changes.

Analyze the implementation details, look for:
- Logical flaws or edge-case regressions.
- Syntax errors or typings mismatches.
- Security vulnerabilities (e.g. command injection, directory traversal).
- Performance bottlenecks or redundant operations.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "passed": true/false,
  "critical_issues": [
    "Error description 1",
    "Error description 2"
  ],
  "suggestions": [
    "Suggestion 1",
    "Suggestion 2"
  ],
  "confidence_rating": 0.0 to 1.0
}
"""


class ReviewerAgent(BaseCouncilAgent):
    """Reviewer Agent auditing code and execution plans."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.REVIEWER,
            model=model,
            provider=provider,
            system_prompt=REVIEWER_SYSTEM_PROMPT,
        )

    async def review(self, task: str, proposal: str, context: str) -> ReviewerMessage:
        """Perform static quality audit on proposed plans or implementations."""
        logger.info("Reviewer auditing proposed implementation...")

        user_messages = [
            {
                "role": "user",
                "content": (
                    f"TASK TO SOLVE: {task}\n\n"
                    f"PROPOSED WORK / CODE:\n{proposal}\n\n"
                    f"ORIGINAL CONTEXT AND FILES:\n{context}"
                ),
            }
        ]

        result = await self.typed_deliberate(user_messages, ReviewerMessage)
        if result.parse_error:
            logger.warning("Reviewer parse failed, using degraded default: %s", result.parse_error)
            # Re-construct suggestions to contain a useful note
            result.suggestions.append(
                f"Review executed but results were unparseable: {result.parse_error}"
            )
        return result
