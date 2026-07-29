"""Anthropic/Claude provider profile — data, not text. See §3.2, §16."""

from __future__ import annotations

from velune.prompt_intelligence.ir import (
    ContextOrdering,
    DelimiterStyle,
    PlanningStyle,
    ProviderProfile,
)

ANTHROPIC_PROFILE = ProviderProfile(
    family="claude",
    delimiter_style=DelimiterStyle.XML,
    context_ordering=ContextOrdering.STABLE_FIRST,
    planning_style=PlanningStyle.EXPLICIT_PHASES,
    max_system_tokens=None,
    supports_prompt_caching=True,
)
