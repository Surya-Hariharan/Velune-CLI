"""Gemini provider profile — data, not text. See §3.2, §16."""

from __future__ import annotations

from velune.prompt_intelligence.ir import (
    ContextOrdering,
    DelimiterStyle,
    PlanningStyle,
    ProviderProfile,
)

GEMINI_PROFILE = ProviderProfile(
    family="gemini",
    delimiter_style=DelimiterStyle.XML,
    context_ordering=ContextOrdering.CONTEXT_FIRST,
    planning_style=PlanningStyle.EXPLICIT_PHASES,
    max_system_tokens=None,
    supports_prompt_caching=False,
    anchor_phrase="Given the context above, respond to the following request:",
)
