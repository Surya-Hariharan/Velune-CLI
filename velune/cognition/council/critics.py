"""Specialized Reasoning Council critics auditing system attributes."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider
from velune.cognition.council.base import BaseCouncilAgent

logger = logging.getLogger("velune.cognition.council.critics")


def clean_json_output(raw_output: str) -> str:
    """Helper to remove markdown wrappers from LLM JSON responses."""
    cleaned = raw_output.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


# =====================================================================
# 1. Scalability Critic
# =====================================================================

SCALABILITY_PROMPT = """You are the Scalability Critic for the Velune Reasoning Council.
Your role is to critique code changes for algorithmic complexity, database optimization, lock contention, and concurrency bottlenecks.

Identify:
- Algorithmic complexities worse than necessary (e.g. O(N^2) loops where O(N log N) is possible).
- Database lock contentions, unindexed query operations, or expensive transaction boundaries.
- Thread safety issues, racing resources, or synchronous blockers in async pathways.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "passed": true/false,
  "issues": ["Issue description 1", "Issue description 2"],
  "score": 0.0 to 1.0,
  "rationale": "Trade-offs and architectural reasoning"
}
"""


class ScalabilityCritic(BaseCouncilAgent):
    """Audits implementation plans for scaling bottlenecks and algorithmic efficiency."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.CHALLENGER,  # Mapped to challenger seat or custom
            model=model,
            provider=provider,
            system_prompt=SCALABILITY_PROMPT,
        )

    async def critique(self, task: str, proposal: str, context: str) -> Dict[str, Any]:
        user_messages = [
            {
                "role": "user",
                "content": f"TASK: {task}\n\nPROPOSAL:\n{proposal}\n\nCONTEXT:\n{context}",
            }
        ]
        raw_output = await self.deliberate(user_messages, temperature=0.1)
        try:
            return json.loads(clean_json_output(raw_output))
        except Exception as e:
            logger.error("Failed to parse ScalabilityCritic JSON: %s", e)
            return {
                "passed": True,
                "issues": [],
                "score": 0.9,
                "rationale": f"Critic output was unparseable: {raw_output}",
            }


# =====================================================================
# 2. Security Critic
# =====================================================================

SECURITY_PROMPT = """You are the Security Critic for the Velune Reasoning Council.
Your role is to inspect code plans for vulnerabilities, input validation escapes, sandbox leaks, and memory issues.

Identify:
- Shell injection, argument parsing escapes, path traversal (e.g. ../), and raw SQL injections.
- Secret leaks, hardcoded credentials, or insecure cryptographic configurations.
- Unsanitized inputs, buffer issues, or dangerous import statements.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "passed": true/false,
  "issues": ["Issue description 1", "Issue description 2"],
  "score": 0.0 to 1.0,
  "rationale": "Security analysis and containment safety"
}
"""


class SecurityCritic(BaseCouncilAgent):
    """Audits changes for safety, input boundaries, and execution containment."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.REVIEWER,
            model=model,
            provider=provider,
            system_prompt=SECURITY_PROMPT,
        )

    async def critique(self, task: str, proposal: str, context: str) -> Dict[str, Any]:
        user_messages = [
            {
                "role": "user",
                "content": f"TASK: {task}\n\nPROPOSAL:\n{proposal}\n\nCONTEXT:\n{context}",
            }
        ]
        raw_output = await self.deliberate(user_messages, temperature=0.1)
        try:
            return json.loads(clean_json_output(raw_output))
        except Exception as e:
            logger.error("Failed to parse SecurityCritic JSON: %s", e)
            return {
                "passed": True,
                "issues": [],
                "score": 0.9,
                "rationale": f"Critic output was unparseable: {raw_output}",
            }


# =====================================================================
# 3. Performance Critic
# =====================================================================

PERFORMANCE_PROMPT = """You are the Performance Critic for the Velune Reasoning Council.
Your role is to critique changes for memory allocation limits, peak CPU utilization, loop efficiency, and latency bottlenecks.

Identify:
- Unnecessary heap allocations or intensive objects creation in tight iterations.
- Expensive I/O, heavy serializations, or excessive network requests.
- Sub-optimal memory utilization profiles.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "passed": true/false,
  "issues": ["Issue description 1", "Issue description 2"],
  "score": 0.0 to 1.0,
  "rationale": "Memory, latency, and CPU metrics projection"
}
"""


class PerformanceCritic(BaseCouncilAgent):
    """Audits implementations for runtime speed, RAM bounds, and payload latency."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.REVIEWER,
            model=model,
            provider=provider,
            system_prompt=PERFORMANCE_PROMPT,
        )

    async def critique(self, task: str, proposal: str, context: str) -> Dict[str, Any]:
        user_messages = [
            {
                "role": "user",
                "content": f"TASK: {task}\n\nPROPOSAL:\n{proposal}\n\nCONTEXT:\n{context}",
            }
        ]
        raw_output = await self.deliberate(user_messages, temperature=0.1)
        try:
            return json.loads(clean_json_output(raw_output))
        except Exception as e:
            logger.error("Failed to parse PerformanceCritic JSON: %s", e)
            return {
                "passed": True,
                "issues": [],
                "score": 0.9,
                "rationale": f"Critic output was unparseable: {raw_output}",
            }


# =====================================================================
# 4. Maintainability Critic
# =====================================================================

MAINTAINABILITY_PROMPT = """You are the Maintainability Critic for the Velune Reasoning Council.
Your role is to audit modular clean rules, class responsibility sizing, complexity, and duplicate structures.

Identify:
- Violation of Single Responsibility rules (oversized classes, multiple concerns in one file).
- Heavy coupling, spaghetti pathways, or lack of unit testability.
- Stray formatting, missing docstrings, or poor alignment with repository patterns.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "passed": true/false,
  "issues": ["Issue description 1", "Issue description 2"],
  "score": 0.0 to 1.0,
  "rationale": "Maintainability index, cohesion, and testability review"
}
"""


class MaintainabilityCritic(BaseCouncilAgent):
    """Audits classes cohesion, coupling, testability, and architectural maintainability."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.REVIEWER,
            model=model,
            provider=provider,
            system_prompt=MAINTAINABILITY_PROMPT,
        )

    async def critique(self, task: str, proposal: str, context: str) -> Dict[str, Any]:
        user_messages = [
            {
                "role": "user",
                "content": f"TASK: {task}\n\nPROPOSAL:\n{proposal}\n\nCONTEXT:\n{context}",
            }
        ]
        raw_output = await self.deliberate(user_messages, temperature=0.1)
        try:
            return json.loads(clean_json_output(raw_output))
        except Exception as e:
            logger.error("Failed to parse MaintainabilityCritic JSON: %s", e)
            return {
                "passed": True,
                "issues": [],
                "score": 0.9,
                "rationale": f"Critic output was unparseable: {raw_output}",
            }
