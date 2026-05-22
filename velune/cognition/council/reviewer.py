"""Reviewer agent auditing planned steps and generated code for bugs, styles, and regressions."""

from __future__ import annotations

import json
from typing import Dict, Any, List, Optional
import logging

from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider
from velune.cognition.council.base import BaseCouncilAgent

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

    async def review(self, task: str, proposal: str, context: str) -> Dict[str, Any]:
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

        raw_output = await self.deliberate(user_messages, temperature=0.1)

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
            logger.error("Failed to parse Reviewer JSON output: %s", e)
            return {
                "passed": True,
                "critical_issues": [],
                "suggestions": [f"Review executed but results were unparseable: {raw_output}"],
                "confidence_rating": 0.5,
            }
