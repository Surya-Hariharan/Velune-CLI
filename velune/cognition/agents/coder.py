"""Production CoderAgent with budget enforcement and state isolation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from velune.cognition.council.base import BaseCouncilAgent
from velune.core.types.model import ModelDescriptor
from velune.execution.edit_formats.registry import parse_with_fallback
from velune.models.family import ModelFamily
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

if TYPE_CHECKING:
    from velune.cognition.state import CouncilState

logger = logging.getLogger("velune.cognition.agents.coder")

CODER_SYSTEM_PROMPT = """You are the Lead Coder for the Velune Reasoning Council.
Your sole mission is to write robust, elegant, production-grade source code.

Follow these strict rules:
1. Always write complete implementations, avoiding placeholders, TODOs, or truncation.
2. Follow professional styling: add thorough docstrings, standard formatting (PEP8 for Python), and type safety definitions.
3. Write clean, self-contained implementations that solve the user's targeted task.
4. Output your proposed changes clearly as unified diffs or file contents, explaining design decisions.
5. Never reference prior feedback or debate loop context — write the best solution from scratch.
"""


class CoderAgent(BaseCouncilAgent):
    """Production Coder Agent with budget enforcement and diff generation."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.CODER,
            model=model,
            provider=provider,
            system_prompt=CODER_SYSTEM_PROMPT,
        )

    async def generate_code(
        self,
        task: str,
        retrieved_context: str,
        plan_context: str,
        state: CouncilState,
        style_profile: dict[str, Any] | None = None,
        reviewer_notes: str = "",
    ) -> list[dict[str, Any]]:
        """Generate code diffs with timeout enforcement from budget.

        Args:
            task: Original task description
            retrieved_context: Repository context
            plan_context: Task plan summary or refinement feedback from reviewer
            state: CouncilState to write diffs into
            style_profile: Style hints for the codebase
            reviewer_notes: Feedback from reviewer (if in revision cycle)

        Returns:
            List of diff dicts with keys: file_path, original, proposed, is_new_file

        Raises:
            TimeoutError: If coder_timeout_seconds exceeded
            ValueError: If budget exhausted before execution
        """
        if state.is_budget_exhausted():
            raise ValueError("Wall-clock budget exhausted before Coder could run")

        remaining = state.remaining_budget_seconds()
        timeout = min(state.budget.coder_timeout_seconds, int(remaining))

        logger.info(
            "Coder starting implementation (timeout: %ds, wall budget: %.1fs remaining)",
            timeout,
            remaining,
        )

        # Build style guidance block
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
                f"Adhere strictly to the following styling patterns:\n"
                f"- **Naming**: `{dominant}`\n"
                f"- **Type Hints**: Strictness `{strictness:.2f}`\n"
                f"- **Paradigm**: `{paradigm}`\n"
                f"- **Docstrings**: `{doc_style}`\n"
                f"- **Constructs**: `{constructs}`\n\n"
            )

        # Build user content with optional reviewer notes
        reviewer_block = ""
        if reviewer_notes:
            reviewer_block = (
                f"### [REVIEWER FEEDBACK]\n"
                f"The reviewer has provided the following guidance:\n{reviewer_notes}\n\n"
                f"Please incorporate this feedback into your revised implementation.\n\n"
            )

        user_content = (
            f"{style_block}"
            f"{reviewer_block}"
            f"TASK: {task}\n\n"
            f"PLAN/CONTEXT:\n{plan_context}\n\n"
            f"CURRENT CODE & CONTEXT:\n{retrieved_context}"
        )

        try:
            import asyncio

            user_messages = [{"role": "user", "content": user_content}]

            # Call deliberate with timeout
            response = await asyncio.wait_for(
                self.deliberate(
                    user_messages,
                    temperature=0.3,
                    max_tokens=state.budget.max_tokens_per_agent,
                ),
                timeout=timeout,
            )

            # Parse response into diffs
            diffs = self._parse_diffs(response)
            state.set_coder_output(diffs)

            logger.info("Coder completed with %d proposed diffs", len(diffs))
            return diffs

        except TimeoutError:
            logger.error("Coder timed out after %ds", timeout)
            raise

    def _parse_diffs(self, response: str) -> list[dict[str, Any]]:
        """Parse agent response into structured diffs via edit_formats registry.

        Tries each format in the model-family preference order (search_replace →
        whole_file → udiff) and returns the first successful parse.  Falls back
        to wrapping the raw response as a single whole-file block so the caller
        always receives a non-empty list.
        """
        family = ModelFamily.UNKNOWN
        if self.model.family:
            try:
                family = ModelFamily(self.model.family.lower())
            except ValueError:
                pass

        blocks = parse_with_fallback(response, family)

        if not blocks:
            logger.warning(
                "No structured edit blocks found in coder response; "
                "wrapping raw output as fallback diff"
            )
            return [
                {
                    "file_path": "generated_output.txt",
                    "original": "",
                    "proposed": response,
                    "is_new_file": True,
                    "is_deletion": False,
                }
            ]

        return [
            {
                "file_path": b.file_path,
                "original": b.original,
                "proposed": b.proposed,
                "is_new_file": b.is_new_file,
                "is_deletion": b.is_deletion,
            }
            for b in blocks
        ]
