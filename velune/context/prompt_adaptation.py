"""Prompt format adaptation for different model families.

Adapts prompts and messages to match the native format of each model family,
improving output quality without requiring model calls. Each family has a
distinct prompt structure and system prompt handling.
"""

from __future__ import annotations

import logging
from abc import ABC
from typing import Protocol

from velune.context.token_counter import estimate_tokens
from velune.core.types.model import ModelDescriptor
from velune.models.family import ModelFamily, detect_family

logger = logging.getLogger("velune.context.prompt_adaptation")


class PromptTemplate(Protocol):
    """Protocol for prompt format templates.

    Each model family has a distinct prompt structure.
    Templates handle formatting messages and converting between formats.
    """

    family: ModelFamily

    # Guidance flags
    prefer_shorter_system_prompts: bool
    supports_xml_structured_output: bool
    supports_json_mode: bool
    max_recommended_system_tokens: int

    def format_system(self, system_content: str) -> str:
        """Format a system prompt according to this family's convention."""
        ...

    def format_user(self, user_content: str) -> str:
        """Format a user message according to this family's convention."""
        ...

    def format_assistant(self, assistant_content: str) -> str:
        """Format an assistant message according to this family's convention."""
        ...

    def format_full_conversation(self, system: str, turns: list[dict[str, str]]) -> str:
        """Format a complete conversation into a single prompt string.

        Used for models that expect a single text input rather than
        message lists. Turns are dicts with 'role' and 'content' keys.
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Concrete Template Implementations
# ─────────────────────────────────────────────────────────────────────────────


class QwenTemplate(ABC):
    """Qwen models use ChatML format with <|im_start|>/<|im_end|> tokens."""

    family = ModelFamily.QWEN
    prefer_shorter_system_prompts = False
    supports_xml_structured_output = True
    supports_json_mode = True
    max_recommended_system_tokens = 4096

    @staticmethod
    def format_system(system_content: str) -> str:
        return f"<|im_start|>system\n{system_content}\n<|im_end|>"

    @staticmethod
    def format_user(user_content: str) -> str:
        return f"<|im_start|>user\n{user_content}\n<|im_end|>"

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return f"<|im_start|>assistant\n{assistant_content}\n<|im_end|>"

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        parts = []
        if system:
            parts.append(QwenTemplate.format_system(system))
        for turn in turns:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role == "user":
                parts.append(QwenTemplate.format_user(content))
            elif role == "assistant":
                parts.append(QwenTemplate.format_assistant(content))
            elif role == "system":
                parts.append(QwenTemplate.format_system(content))
        return "\n".join(parts) + "\n<|im_start|>assistant\n"


class DeepSeekTemplate(ABC):
    """DeepSeek models support reasoning with special thinking tokens."""

    family = ModelFamily.DEEPSEEK
    prefer_shorter_system_prompts = False
    supports_xml_structured_output = True
    supports_json_mode = True
    max_recommended_system_tokens = 4096

    @staticmethod
    def format_system(system_content: str) -> str:
        return f"System: {system_content}"

    @staticmethod
    def format_user(user_content: str) -> str:
        return f"User: {user_content}"

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return f"Assistant: {assistant_content}"

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        parts = []
        if system:
            parts.append(DeepSeekTemplate.format_system(system))
        for turn in turns:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role == "user":
                parts.append(DeepSeekTemplate.format_user(content))
            elif role == "assistant":
                parts.append(DeepSeekTemplate.format_assistant(content))
            elif role == "system":
                parts.append(DeepSeekTemplate.format_system(content))
        return "\n\n".join(parts) + "\n\nAssistant: "


class Llama3Template(ABC):
    """Llama3 uses [INST] / <<SYS>> markers for instruction and system prompts."""

    family = ModelFamily.LLAMA3
    prefer_shorter_system_prompts = False
    supports_xml_structured_output = True
    supports_json_mode = False
    max_recommended_system_tokens = 2048

    @staticmethod
    def format_system(system_content: str) -> str:
        return f"<<SYS>>\n{system_content}\n<</SYS>>"

    @staticmethod
    def format_user(user_content: str) -> str:
        return f"[INST] {user_content} [/INST]"

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return assistant_content

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        parts = []
        system_marker = f"\n{Llama3Template.format_system(system)}\n" if system else ""

        for i, turn in enumerate(turns):
            role = turn.get("role", "user")
            content = turn.get("content", "")

            if role == "user":
                if i == 0 and system:
                    # First user turn gets the system prompt embedded
                    parts.append(f"[INST] {system_marker}{content} [/INST]")
                else:
                    parts.append(f"[INST] {content} [/INST]")
            elif role == "assistant":
                parts.append(f" {content} ")

        return "".join(parts)


class PhiTemplate(ABC):
    """Phi models respond better to shorter, more direct prompts."""

    family = ModelFamily.PHI
    prefer_shorter_system_prompts = True
    supports_xml_structured_output = False
    supports_json_mode = False
    max_recommended_system_tokens = 512

    @staticmethod
    def format_system(system_content: str) -> str:
        return system_content

    @staticmethod
    def format_user(user_content: str) -> str:
        return user_content

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return assistant_content

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        parts = []
        if system:
            parts.append(system)
        for turn in turns:
            content = turn.get("content", "")
            if content:
                parts.append(content)
        return "\n\n".join(parts)


class MistralTemplate(ABC):
    """Mistral uses [INST] markers but places system prompt differently."""

    family = ModelFamily.MISTRAL
    prefer_shorter_system_prompts = False
    supports_xml_structured_output = False
    supports_json_mode = False
    max_recommended_system_tokens = 2048

    @staticmethod
    def format_system(system_content: str) -> str:
        return f"[SYSTEM] {system_content}"

    @staticmethod
    def format_user(user_content: str) -> str:
        return f"[INST] {user_content} [/INST]"

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return assistant_content

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        parts = []
        if system:
            parts.append(MistralTemplate.format_system(system))

        for turn in turns:
            role = turn.get("role", "user")
            content = turn.get("content", "")

            if role == "user":
                parts.append(f"[INST] {content} [/INST]")
            elif role == "assistant":
                parts.append(f" {content}")

        return " ".join(parts)


class GemmaTemplate(ABC):
    """Gemma is similar to Llama3 with a direct message format."""

    family = ModelFamily.GEMMA
    prefer_shorter_system_prompts = False
    supports_xml_structured_output = False
    supports_json_mode = False
    max_recommended_system_tokens = 2048

    @staticmethod
    def format_system(system_content: str) -> str:
        return system_content

    @staticmethod
    def format_user(user_content: str) -> str:
        return f"<start_of_turn>user\n{user_content}<end_of_turn>"

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return f"<start_of_turn>model\n{assistant_content}<end_of_turn>"

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        parts = []
        if system:
            parts.append(f"<start_of_turn>user\n{system}<end_of_turn>")

        for turn in turns:
            role = turn.get("role", "user")
            content = turn.get("content", "")

            if role == "user":
                parts.append(f"<start_of_turn>user\n{content}<end_of_turn>")
            elif role == "assistant":
                parts.append(f"<start_of_turn>model\n{content}<end_of_turn>")

        parts.append("<start_of_turn>model\n")
        return "".join(parts)


class StandardAPITemplate(ABC):
    """Standard API format for Claude, GPT, Gemini, etc.

    Uses message lists with role/content pairs.
    System prompt is a separate message with role="system".
    """

    family = ModelFamily.UNKNOWN
    prefer_shorter_system_prompts = False
    supports_xml_structured_output = True
    supports_json_mode = True
    max_recommended_system_tokens = 4096

    @staticmethod
    def format_system(system_content: str) -> str:
        return system_content

    @staticmethod
    def format_user(user_content: str) -> str:
        return user_content

    @staticmethod
    def format_assistant(assistant_content: str) -> str:
        return assistant_content

    @staticmethod
    def format_full_conversation(system: str, turns: list[dict[str, str]]) -> str:
        # For standard API, this just reconstructs the message list as text
        # (mainly for debugging or conversion)
        parts = []
        if system:
            parts.append(f"System: {system}")
        for turn in turns:
            role = turn.get("role", "unknown")
            content = turn.get("content", "")
            parts.append(f"{role.capitalize()}: {content}")
        return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Adaptation Engine
# ─────────────────────────────────────────────────────────────────────────────


class PromptAdaptationEngine:
    """Adapts prompts and messages to match each model family's format.

    Handles prompt reformatting, system prompt truncation, and message
    list conversion for different model families.
    """

    def __init__(self) -> None:
        """Initialize with template implementations for each family."""
        self._templates = {
            ModelFamily.QWEN: QwenTemplate,
            ModelFamily.DEEPSEEK: DeepSeekTemplate,
            ModelFamily.LLAMA3: Llama3Template,
            ModelFamily.PHI: PhiTemplate,
            ModelFamily.MISTRAL: MistralTemplate,
            ModelFamily.GEMMA: GemmaTemplate,
            ModelFamily.CLAUDE: StandardAPITemplate,
            ModelFamily.GPT: StandardAPITemplate,
            ModelFamily.GEMINI: StandardAPITemplate,
            ModelFamily.UNKNOWN: StandardAPITemplate,
        }

    def get_template(self, model: ModelDescriptor) -> type:
        """Get the prompt template for a model.

        Parameters
        ----------
        model:
            The model descriptor.

        Returns
        -------
        type:
            The template class for the model's family.
        """
        family = detect_family(model.model_id)
        return self._templates.get(family, StandardAPITemplate)

    def adapt_system_prompt(self, system: str, model: ModelDescriptor) -> str:
        """Adapt and truncate a system prompt to match the model's preferences.

        Parameters
        ----------
        system:
            The original system prompt.
        model:
            The target model descriptor.

        Returns
        -------
        str:
            The adapted system prompt, truncated if needed.
        """
        if not system:
            return ""

        template = self.get_template(model)
        max_tokens = template.max_recommended_system_tokens

        # Check if truncation is needed
        token_count = estimate_tokens(system)
        if token_count > max_tokens:
            # Truncate to fit within budget
            target_char_count = max_tokens * 4  # Rough estimate
            truncated = system[:target_char_count].rsplit(" ", 1)[0]
            truncated += "\n[... truncated due to model constraints ...]"
            logger.debug(
                "System prompt truncated from %d to %d tokens for %s",
                token_count,
                estimate_tokens(truncated),
                model.model_id,
            )
            return truncated

        return system

    def adapt_messages(
        self, messages: list[dict[str, str]], model: ModelDescriptor
    ) -> list[dict[str, str]]:
        """Adapt message list to match the model's expected format.

        For models with specialized formats (Qwen, DeepSeek, Llama3, etc.),
        this converts the standard message list into the model's native format.

        For cloud APIs (Claude, GPT, Gemini), the message list is returned
        unchanged since they already use the standard format.

        Parameters
        ----------
        messages:
            Standard message list with 'role' and 'content' keys.
        model:
            The target model descriptor.

        Returns
        -------
        list[dict[str, str]]:
            Messages in the format expected by the model.
        """
        family = detect_family(model.model_id)

        # Cloud APIs and unknown models use standard message format; return as-is
        if family in (ModelFamily.CLAUDE, ModelFamily.GPT, ModelFamily.GEMINI, ModelFamily.UNKNOWN):
            return messages

        # For specialized formats, wrap in a single "text" message
        # (Some local models expect a single text prompt)
        template = self._templates[family]
        system = ""
        turns = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system = content
            else:
                turns.append({"role": role, "content": content})

        # Format as complete conversation text
        formatted_text = template.format_full_conversation(system, turns)

        return [{"role": "user", "content": formatted_text}]

    def get_capabilities(self, model: ModelDescriptor) -> dict[str, bool | int]:
        """Get formatting capabilities for a model.

        Parameters
        ----------
        model:
            The model descriptor.

        Returns
        -------
        dict:
            Capabilities including XML support, JSON mode, max system tokens.
        """
        template = self.get_template(model)
        return {
            "prefer_shorter_prompts": template.prefer_shorter_system_prompts,
            "supports_xml": template.supports_xml_structured_output,
            "supports_json_mode": template.supports_json_mode,
            "max_system_tokens": template.max_recommended_system_tokens,
        }
