"""Abstract base class for Reasoning Council agents with specialized prompts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Type, TypeVar
from pydantic import BaseModel
T = TypeVar('T', bound=BaseModel)
import logging

from velune.models.specializations import CouncilRole
from velune.core.types.model import ModelDescriptor
from velune.providers.base import ModelProvider
from velune.core.types.inference import InferenceRequest

logger = logging.getLogger("velune.cognition.council.base")


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
        context_history: List[Dict[str, str]],
        temperature: float = 0.5,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Runs the deliberation round using the assigned LLM model provider."""
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

        try:
            logger.info("Agent %s (%s) initiating inference...", self.role.value, self.model.model_id)
            response = await self.provider.infer(request)
            return response.content
        except Exception as e:
            logger.error("deliberation failed for agent %s: %s", self.role.value, e)
            return f"Deliberation failure inside agent {self.role.value}: {e}"

    async def typed_deliberate(
        self,
        context_history: List[Dict[str, str]],
        response_type: Type[T],
        temperature: float = 0.5,
        max_tokens: Optional[int] = None,
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
