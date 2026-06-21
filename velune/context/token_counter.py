"""Token counter with model-aware encoding support.

Supports accurate token counting for OpenAI-family models via tiktoken,
with conservative fallback for other model families.
"""

from __future__ import annotations

import logging
from typing import Any

from velune.core.types.model import ModelDescriptor
from velune.models.family import ModelFamily, detect_family

logger = logging.getLogger("velune.context.token_counter")

# Attempt to load tiktoken for accurate OpenAI-family token counting
try:
    import tiktoken

    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False
    logger.warning("tiktoken not available; using heuristic token estimation")


class TokenCounter:
    """Token counting with model-specific encoding support."""

    # Tiktoken encoding registry for OpenAI-family models
    _ENCODING_CACHE: dict[str, Any] = {}

    @staticmethod
    def count(text: str, model: ModelDescriptor) -> int:
        """Count tokens in text for the given model.

        Args:
            text: Text to count tokens in
            model: ModelDescriptor with model_id and family information

        Returns:
            Estimated or exact token count
        """
        if not text:
            return 0

        # Detect model family if not specified
        family = detect_family(model.model_id)

        # OpenAI-family models: use tiktoken for accuracy
        if family in (ModelFamily.CLAUDE, ModelFamily.GPT):
            return TokenCounter._count_openai_family(text, model.model_id)

        # Other families: conservative heuristic
        return TokenCounter._count_heuristic(text)

    @staticmethod
    def count_messages(messages: list[dict[str, str]], model: ModelDescriptor) -> int:
        """Count tokens in a list of messages.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            model: ModelDescriptor for encoding selection

        Returns:
            Total token count including overhead for message structure
        """
        if not messages:
            return 0

        # Message structure overhead: ~4 tokens per message for role markers
        structure_overhead = len(messages) * 4

        # Count tokens in all message content
        content_tokens = sum(TokenCounter.count(msg.get("content", ""), model) for msg in messages)

        return structure_overhead + content_tokens

    @staticmethod
    def _count_openai_family(text: str, model_id: str) -> int:
        """Count tokens using tiktoken for OpenAI-family models.

        Falls back to heuristic if tiktoken unavailable.
        """
        if not HAS_TIKTOKEN:
            return TokenCounter._count_heuristic(text)

        try:
            # Determine appropriate encoding
            encoding_name = TokenCounter._select_encoding(model_id)
            if encoding_name not in TokenCounter._ENCODING_CACHE:
                TokenCounter._ENCODING_CACHE[encoding_name] = tiktoken.get_encoding(encoding_name)
            encoding = TokenCounter._ENCODING_CACHE[encoding_name]

            # Count tokens, allowing special characters
            return len(encoding.encode(text, disallowed_special=()))
        except Exception as e:
            logger.debug(f"tiktoken counting failed for {model_id}: {e}; falling back to heuristic")
            return TokenCounter._count_heuristic(text)

    @staticmethod
    def _count_heuristic(text: str) -> int:
        """Conservative token estimation for non-OpenAI models.

        Assumes 1 token ≈ 1.35 words (English language average).
        Used as fallback when tiktoken unavailable or for non-OpenAI models.
        """
        if not text:
            return 0
        word_count = len(text.split())
        return max(1, int(word_count * 1.35))

    @staticmethod
    def _select_encoding(model_id: str) -> str:
        """Select appropriate tiktoken encoding based on model.

        Args:
            model_id: Model identifier (e.g., "gpt-4-turbo", "gpt-3.5-turbo")

        Returns:
            Tiktoken encoding name (e.g., "cl100k_base", "o200k_base")
        """
        model_lower = model_id.lower()

        # o1 and newer models: o200k_base
        if "o1" in model_lower or "o200k" in model_lower:
            return "o200k_base"

        # GPT-4-turbo and newer: cl100k_base
        if "gpt-4" in model_lower or "gpt-4-turbo" in model_lower:
            return "cl100k_base"

        # GPT-3.5 and other models: cl100k_base
        return "cl100k_base"


# ── Module-level convenience function ────────────────────────────────────────

try:
    import tiktoken as _tiktoken

    _GLOBAL_ENCODING = _tiktoken.get_encoding("cl100k_base")
    _HAS_TIKTOKEN = True
except Exception:
    _GLOBAL_ENCODING = None
    _HAS_TIKTOKEN = False


def estimate_tokens(text: str) -> int:
    """Estimate token count for *text* without requiring a ModelDescriptor.

    Uses tiktoken (cl100k_base) when available; falls back to a 4-chars-per-token
    heuristic.  This is a drop-in replacement for the legacy
    ``velune.context.window.estimate_tokens`` function.
    """
    if not text:
        return 0
    if _HAS_TIKTOKEN and _GLOBAL_ENCODING is not None:
        try:
            return len(_GLOBAL_ENCODING.encode(text, disallowed_special=()))
        except Exception:
            pass
    return max(1, len(text) // 4 + (1 if len(text) % 4 > 0 else 0))
