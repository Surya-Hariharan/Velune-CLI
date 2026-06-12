"""Context window and token tracking utilities."""

from __future__ import annotations

import logging

logger = logging.getLogger("velune.context.window")

# Attempt to load tiktoken, fallback to heuristic if unavailable
try:
    import tiktoken

    _encoding = tiktoken.get_encoding("cl100k_base")
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False
    _encoding = None


def estimate_tokens(text: str) -> int:
    """Accurately count or estimate tokens in a given text block."""
    if not text:
        return 0
    if HAS_TIKTOKEN and _encoding is not None:
        try:
            return len(_encoding.encode(text, disallowed_special=()))
        except Exception:
            pass
    # Fallback heuristic: 1 token is roughly 4 characters
    return len(text) // 4 + (1 if len(text) % 4 > 0 else 0)


class ContextWindowTracker:
    """Tracks token consumption dynamically against a maximum budget capacity."""

    def __init__(self, max_tokens: int = 8192) -> None:
        self.max_tokens = max_tokens
        self.current_tokens = 0
        self.segments: dict[str, int] = {}

    def reserve(self, segment_name: str, text: str) -> int:
        """Reserve token budget for a specific context segment."""
        tokens = estimate_tokens(text)
        self.segments[segment_name] = tokens
        self._recalculate()
        return tokens

    def release(self, segment_name: str) -> None:
        """Release reservation for a context segment."""
        self.segments.pop(segment_name, None)
        self._recalculate()

    def get_remaining(self) -> int:
        """Get remaining available token budget."""
        return max(0, self.max_tokens - self.current_tokens)

    def is_overflowed(self) -> bool:
        """Check if current reservations exceed max tokens."""
        return self.current_tokens > self.max_tokens

    def _recalculate(self) -> None:
        self.current_tokens = sum(self.segments.values())
        logger.debug("Recalculated tokens: %d/%d", self.current_tokens, self.max_tokens)
