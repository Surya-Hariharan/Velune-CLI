"""Abstract base class for Reasoning Council agents with specialized prompts."""

from __future__ import annotations

from abc import ABC
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar('T', bound=BaseModel)

from velune.core.trace import TracedLogger
from velune.core.types.inference import InferenceRequest
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = TracedLogger("velune.cognition.council.base")


class BaseCouncilAgent(ABC):
    """Base interface for specialized deliberation models within the Reasoning Council."""

    def __init__(
        self,
        role: CouncilRole,
        model: ModelDescriptor,
        provider: ModelProvider,
        system_prompt: str,
    ) -> None:
        self.role = role
        self.model = model
        self.provider = provider
        self.system_prompt = system_prompt

    async def deliberate(
        self,
        context_history: list[dict[str, str]],
        temperature: float = 0.5,
        max_tokens: int | None = None,
    ) -> str:
        """Runs the deliberation round using the assigned LLM model provider."""
        import asyncio

        from velune.core.trace import TraceContext, _run_id

        with TraceContext(
            run_id=_run_id.get() or "unknown",
            agent_id=self.role.value,
        ):
            messages = [{"role": "system", "content": self.system_prompt}] + context_history

            from velune.cognition.firewall import CognitiveFirewall
            firewall = CognitiveFirewall()
            if not firewall.scan_conversation(messages):
                logger.error("Prompt injection detected in Council message history")
                raise ValueError("Security: Potential prompt injection detected in messages")

            request = InferenceRequest(
                model_id=self.model.model_id,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            AGENT_TIMEOUTS = {
                CouncilRole.PLANNER: 120.0,
                CouncilRole.CODER: 180.0,
                CouncilRole.REVIEWER: 120.0,
                CouncilRole.CHALLENGER: 90.0,
                CouncilRole.SYNTHESIZER: 120.0,
            }

            timeout = AGENT_TIMEOUTS.get(self.role, 120.0)

            import time
            start = time.perf_counter()
            try:
                logger.info("Agent %s (%s) initiating inference...", self.role.value, self.model.model_id)
                response = await asyncio.wait_for(
                    self.provider.infer(request),
                    timeout=timeout,
                )
                elapsed = time.perf_counter() - start
                logger.info(
                    "Agent %s completed in %.1fs (%d chars)",
                    self.role.value, elapsed, len(response.content)
                )
                if elapsed > 60.0:
                    logger.warning(
                        "Agent %s took %.1fs (>60s)",
                        self.role.value, elapsed
                    )
                return response.content
            except TimeoutError:
                logger.error("Agent %s timed out after %.0fs", self.role.value, timeout)
                return f"[Agent {self.role.value} timed out — using empty response]"
            except Exception as e:
                logger.error("deliberation failed for agent %s: %s", self.role.value, e)
                return f"Deliberation failure inside agent {self.role.value}: {e}"

    async def typed_deliberate(
        self,
        context_history: list[dict[str, str]],
        response_type: type[T],
        temperature: float = 0.5,
        max_tokens: int | None = None,
    ) -> T:
        """Runs deliberation and parses the output into a strongly-typed Pydantic model."""
        from pydantic import ValidationError

        raw = await self.deliberate(context_history, temperature, max_tokens)
        cleaned = raw.strip()
        for prefix in ("```json", "```"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return response_type.model_validate_json(cleaned)
        except (ValidationError, Exception) as e:
            logger.error(
                "Agent %s returned unparseable response: %s\nRaw: %s",
                self.role.value, e, raw[:200]
            )
            try:
                return response_type.model_construct(parse_error=str(e))
            except Exception:
                raise
