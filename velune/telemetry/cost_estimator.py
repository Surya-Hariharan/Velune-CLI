"""Pre-operation cost estimation for cloud model calls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.core.types.model import ModelDescriptor

from velune.telemetry.token_tracker import PROVIDER_COSTS

logger = logging.getLogger("velune.telemetry.cost_estimator")

_LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "llamacpp"})
_OPENAI_FAMILY = frozenset({"openai", "together", "fireworks", "openrouter"})


class CostEstimator:
    """Estimates token count and USD cost before a model call — no inference required."""

    def estimate_tokens(self, messages: list[dict], model: ModelDescriptor) -> int:
        """Fast token approximation. Uses tiktoken for OpenAI-family; word*1.3 otherwise."""
        if model.provider_id in _OPENAI_FAMILY:
            try:
                import tiktoken  # type: ignore[import-untyped]

                try:
                    enc = tiktoken.encoding_for_model(model.model_id)
                except KeyError:
                    enc = tiktoken.get_encoding("cl100k_base")
                total = 0
                for msg in messages:
                    content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                    total += len(enc.encode(content)) + 4  # +4 for role/separator overhead
                return total
            except ImportError:
                logger.debug("tiktoken not installed; falling back to word-count approximation")

        # Conservative approximation for all other providers
        total_words = 0
        for msg in messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            total_words += len(content.split())
        return max(1, int(total_words * 1.3))

    def estimate_cost(self, token_count: int, model: ModelDescriptor) -> float | None:
        """Returns None for local/free models; USD float for cloud models.

        Priority: model.cost_per_1k_tokens field → PROVIDER_COSTS lookup.
        Returns None if the cost is genuinely unknown (not just zero).
        """
        if getattr(model, "is_local", False) or model.provider_id in _LOCAL_PROVIDERS:
            return None

        # Field on the descriptor takes precedence
        rate: float | None = getattr(model, "cost_per_1k_tokens", None)

        # Fall back to static lookup table
        if rate is None:
            provider_table = PROVIDER_COSTS.get(model.provider_id, {})
            rate = provider_table.get(model.model_id)

        if rate is None:
            return None  # Unknown cost — caller decides how to handle

        return (token_count / 1000) * rate

    def format_estimate(
        self,
        token_count: int,
        cost: float | None,
        model: ModelDescriptor | None = None,
    ) -> str:
        """Returns a human-readable estimate string.

        Examples:
            "~4,200 tokens (~$0.021 with claude-sonnet-4-6)"
            "~4,200 tokens (local, no cost)"
            "~4,200 tokens (cost unknown)"
        """
        token_str = f"~{token_count:,} tokens"

        if cost is None:
            if model and (
                getattr(model, "is_local", False) or model.provider_id in _LOCAL_PROVIDERS
            ):
                return f"{token_str} (local, no cost)"
            return f"{token_str} (cost unknown)"

        model_label = model.model_id if model else "unknown model"
        return f"{token_str} (~${cost:.3f} with {model_label})"
