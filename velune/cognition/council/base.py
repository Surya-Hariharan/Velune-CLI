"""Abstract base class for Reasoning Council agents with specialized prompts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
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
