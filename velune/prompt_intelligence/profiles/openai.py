"""OpenAI/GPT provider profile — data, not text. See §3.2, §16."""

from __future__ import annotations

from velune.prompt_intelligence.ir import (
    ContextOrdering,
    DelimiterStyle,
    PlanningStyle,
    ProviderProfile,
)

OPENAI_PROFILE = ProviderProfile(
    family="gpt",
    delimiter_style=DelimiterStyle.MARKDOWN,
    context_ordering=ContextOrdering.ROLE_TASK_CONTEXT,
    planning_style=PlanningStyle.LIGHT_TOUCH,
    max_system_tokens=None,
    supports_prompt_caching=False,
)
