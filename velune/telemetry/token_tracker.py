"""Token usage tracking and cost estimation for all inference calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# Per-provider cost table (USD per 1 000 tokens, blended input+output rate).
# Zero means free tier.
# ---------------------------------------------------------------------------
PROVIDER_COSTS: dict[str, dict[str, float]] = {
    "anthropic": {
        "claude-opus-4-5": 0.015,
        "claude-sonnet-4-5": 0.003,
        "claude-haiku-4-5": 0.00025,
    },
    "openai": {
        "gpt-4o": 0.005,
        "gpt-4o-mini": 0.00015,
        "o1": 0.015,
        "o1-mini": 0.003,
    },
    "xai": {
        "grok-2": 0.002,
        "grok-2-mini": 0.0002,
    },
    "google": {
        "gemini-2.0-flash": 0.000075,
        "gemini-1.5-pro": 0.00125,
        "gemini-1.5-flash": 0.000075,
        "gemini-2.0-flash-thinking-exp": 0.0,
    },
    "groq": {
        # All free tier. mixtral-8x7b-32768, gemma2-9b-it, and
        # llama-3.2-11b-vision-preview were decommissioned by Groq (2026-07)
        # and removed — see providers/adapters/groq.py.
        "llama-3.3-70b-versatile": 0.0,
        "llama-3.1-8b-instant": 0.0,
        "openai/gpt-oss-120b": 0.0,
        "qwen/qwen3-32b": 0.0,
    },
    "together": {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": 0.00088,
        "meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo": 0.00018,
        "Qwen/Qwen2.5-Coder-32B-Instruct": 0.0008,
        "deepseek-ai/DeepSeek-R1": 0.003,
        "mistralai/Mistral-7B-Instruct-v0.3": 0.0002,
    },
    "fireworks": {
        "accounts/fireworks/models/llama-v3p3-70b-instruct": 0.0009,
        "accounts/fireworks/models/deepseek-r1": 0.003,
        "accounts/fireworks/models/qwen2p5-coder-32b-instruct": 0.0009,
        "accounts/fireworks/models/mixtral-8x22b-instruct": 0.0009,
    },
    "openrouter": {},  # Dynamic — set from model metadata
    "meta": {},  # Preview pricing not yet finalized publicly — treat as unknown, not free
    "zai": {
        "glm-4.5-air": 0.0,  # free tier
    },
    "ollama": {},  # Always free (local)
    "lmstudio": {},  # Always free (local)
    "llamacpp": {},  # Always free (local)
}


@dataclass
class TokenUsage:
    model_id: str
    provider_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    timestamp: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_response(
        cls,
        provider_id: str,
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> TokenUsage:
        total = prompt_tokens + completion_tokens
        cost = cls._calculate_cost(provider_id, model_id, total)
        return cls(
            model_id=model_id,
            provider_id=provider_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            cost_usd=cost,
        )

    @staticmethod
    def _calculate_cost(provider_id: str, model_id: str, tokens: int) -> float:
        rate = PROVIDER_COSTS.get(provider_id, {}).get(model_id, 0.0)
        return (tokens / 1000) * rate


@dataclass
class SessionUsage:
    usages: list[TokenUsage] = field(default_factory=list)

    def add(self, usage: TokenUsage) -> None:
        self.usages.append(usage)

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.usages)

    @property
    def total_cost(self) -> float:
        return sum(u.cost_usd for u in self.usages)

    @property
    def prompt_tokens(self) -> int:
        return sum(u.prompt_tokens for u in self.usages)

    @property
    def completion_tokens(self) -> int:
        return sum(u.completion_tokens for u in self.usages)

    def by_provider(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for u in self.usages:
            result[u.provider_id] = result.get(u.provider_id, 0) + u.total_tokens
        return result

    def summary_line(self) -> str:
        parts = [f"{self.total_tokens:,} tokens"]
        if self.total_cost > 0:
            parts.append(f"~${self.total_cost:.4f}")
        else:
            parts.append("free")
        parts.append("session total")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Process-level session tracker — populated by inference call sites
# ---------------------------------------------------------------------------

current_session: SessionUsage = SessionUsage()
