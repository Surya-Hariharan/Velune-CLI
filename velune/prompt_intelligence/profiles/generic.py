"""Generic/fallback provider profile — data, not text. See §3.2, §14, §16.

Used for any family without a registered profile (local models: Qwen,
DeepSeek, Llama3, Phi, Mistral, Gemma, and UNKNOWN). The 2048-token system
ceiling mirrors the deleted PromptAdaptationEngine's per-family caps (e.g.
Phi's 512-token limit) generalized to one conservative default — see
docs/02-prompt-intelligence.md §0.
"""

from __future__ import annotations

from velune.prompt_intelligence.ir import (
    ContextOrdering,
    DelimiterStyle,
    PlanningStyle,
    ProviderProfile,
)

GENERIC_PROFILE = ProviderProfile(
    family="unknown",
    delimiter_style=DelimiterStyle.PLAIN,
    context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
    planning_style=PlanningStyle.NONE,
    max_system_tokens=2048,
    supports_prompt_caching=False,
)
