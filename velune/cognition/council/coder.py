"""Coder agent specializing in clean, robust code generation and modifications."""

from __future__ import annotations

import logging
from typing import Any

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.prompts import COUNCIL_CODER, get_prompt
from velune.core.types.model import ModelDescriptor
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council.coder")

CODER_SYSTEM_PROMPT = get_prompt(COUNCIL_CODER)


class CoderAgent(BaseCouncilAgent):
    """Coder Agent for the Reasoning Council."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.CODER,
            model=model,
            provider=provider,
            system_prompt=CODER_SYSTEM_PROMPT,
        )

    async def write_code(
        self,
        prompt: str,
        current_code: str,
        plan_context: str,
        style_profile: dict[str, Any] | None = None,
        format_instructions: str = "",
        temperature: float | None = None,
    ) -> str:
        """Emits concrete code implementations aligned with codebase styling conventions.

        ``temperature`` overrides the role default so the orchestrator can draw
        several divergent candidate solutions (multi-solver self-consistency).
        """
        logger.info("Coder generating code changes...")

        style_block = ""
        if style_profile:
            naming = style_profile.get("naming_conventions", {})
            dominant = naming.get("dominant", "Hybrid")
            strictness = style_profile.get("type_hinting_strictness", 1.0)
            paradigm = style_profile.get("class_vs_functional", "Hybrid")
            doc_style = style_profile.get("docstring_style", "Google")
            constructs = ", ".join(style_profile.get("preferred_constructs", []))

            style_block = (
                f"### [COGNITIVE STYLE ENFORCEMENT]\n"
                f"Adhere strictly to the following styling patterns of the target repository/directory:\n"
                f"- **Naming Conventions**: Dominant style is `{dominant}`.\n"
                f"- **Type Hinting Strictness**: Score: `{strictness:.2f}` (Ensure parameters and return types match this strictness level).\n"
                f"- **Programming Paradigm**: Preferred style is `{paradigm}` (OOP, Functional, or Hybrid).\n"
                f"- **Docstring Convention**: Use `{doc_style}` format (e.g. Google-style 'Args:' and 'Returns:' or Sphinx-style ':param').\n"
                f"- **Preferred Constructs/Libraries**: `{constructs}`.\n\n"
            )

        format_block = f"{format_instructions}\n" if format_instructions else ""

        user_content = (
            style_block
            + format_block
            + (
                f"GOAL: {prompt}\n\n"
                f"ACTIVE EXECUTION STAGE PLAN:\n{plan_context}\n\n"
                f"CURRENT FILE CONTENTS / CONTEXT:\n{current_code}"
            )
        )

        user_messages = [
            {
                "role": "user",
                "content": user_content,
            }
        ]

        return await self.deliberate(user_messages, temperature=temperature)
