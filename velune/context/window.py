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


# ── Chat-turn conversation windowing ────────────────────────────────────────

# Per-message wire overhead (role tags, separators) in tokens — the OpenAI
# chat-format constant; close enough for every provider Velune targets.
_MESSAGE_OVERHEAD_TOKENS = 4


def _message_tokens(message: dict) -> int:
    content = message.get("content")
    text = content if isinstance(content, str) else str(content or "")
    # Assistant tool_calls entries carry their payload outside `content`.
    if message.get("tool_calls"):
        text += str(message["tool_calls"])
    return estimate_tokens(text) + _MESSAGE_OVERHEAD_TOKENS


def fit_messages(messages: list[dict], max_input_tokens: int) -> list[dict]:
    """Return the newest suffix of *messages* that fits *max_input_tokens*.

    Replaces the old fixed ``conversation[-50:]`` slice: history is fitted to
    the model's actual input budget (see
    :meth:`velune.context.budget.ContextBudget.for_chat`) so small local
    models never overflow their window and large models keep far more history
    than 50 messages.

    Walks backwards from the most recent message, keeping whole messages until
    the budget is exhausted. The final message (the prompt being answered) is
    always kept even if it alone exceeds the budget — the provider is the
    right place to fail on a single oversized message, not silent truncation.

    The returned window never *starts* with an orphaned ``role="tool"``
    result (whose assistant ``tool_calls`` antecedent was cut), so provider
    message-ordering validation always passes.
    """
    if not messages:
        return []

    kept: list[dict] = []
    remaining = max(0, max_input_tokens)
    for message in reversed(messages):
        cost = _message_tokens(message)
        if kept and cost > remaining:
            break
        kept.append(message)
        remaining -= cost
    kept.reverse()

    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept
