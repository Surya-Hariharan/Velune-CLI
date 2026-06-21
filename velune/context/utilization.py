from __future__ import annotations

from velune.context.token_counter import estimate_tokens


class ContextUtilizationTracker:
    """Tracks token consumption dynamically and reports utilization percentages."""

    def __init__(self, max_tokens: int = 8192) -> None:
        self.max_tokens = max_tokens
        self.used_tokens = 0

    def update(self, conversation: list[dict] | str) -> None:
        """Update the used token count based on active conversation list or raw string."""
        if isinstance(conversation, str):
            self.used_tokens = estimate_tokens(conversation)
        elif isinstance(conversation, list):
            text = " ".join(m.get("content", "") for m in conversation if isinstance(m, dict))
            self.used_tokens = estimate_tokens(text)
        else:
            self.used_tokens = 0

    @property
    def percentage(self) -> float:
        """Returns the percentage of utilization, from 0.0 to 100.0."""
        if self.max_tokens <= 0:
            return 0.0
        return min((self.used_tokens / self.max_tokens) * 100.0, 100.0)

    @property
    def formatted_badge(self) -> str:
        """Returns a string in the format [ctx:NN%]."""
        return f"[ctx:{self.percentage:.0f}%]"
