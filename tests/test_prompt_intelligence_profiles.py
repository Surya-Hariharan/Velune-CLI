"""Provider profile registry — see docs/02-prompt-intelligence.md §17.

Adding a new provider must be one register_provider_profile() call; unknown
families must always fall back to GENERIC_PROFILE rather than raising.
"""

from __future__ import annotations

from velune.models.family import ModelFamily
from velune.prompt_intelligence.ir import ContextOrdering, DelimiterStyle
from velune.prompt_intelligence.profiles import (
    ANTHROPIC_PROFILE,
    GEMINI_PROFILE,
    GENERIC_PROFILE,
    OPENAI_PROFILE,
    get_provider_profile,
    register_provider_profile,
)


def test_claude_resolves_to_anthropic_profile():
    assert get_provider_profile(ModelFamily.CLAUDE) is ANTHROPIC_PROFILE
    assert ANTHROPIC_PROFILE.delimiter_style == DelimiterStyle.XML
    assert ANTHROPIC_PROFILE.supports_prompt_caching is True


def test_gpt_resolves_to_openai_profile():
    assert get_provider_profile(ModelFamily.GPT) is OPENAI_PROFILE
    assert OPENAI_PROFILE.delimiter_style == DelimiterStyle.MARKDOWN


def test_gemini_resolves_to_gemini_profile_with_anchor_phrase():
    assert get_provider_profile(ModelFamily.GEMINI) is GEMINI_PROFILE
    assert GEMINI_PROFILE.context_ordering == ContextOrdering.CONTEXT_FIRST
    assert GEMINI_PROFILE.anchor_phrase


def test_unregistered_family_falls_back_to_generic_profile():
    for family in (ModelFamily.QWEN, ModelFamily.LLAMA3, ModelFamily.UNKNOWN):
        assert get_provider_profile(family) is GENERIC_PROFILE


def test_register_provider_profile_is_additive_extensibility_hook():
    custom = ANTHROPIC_PROFILE  # any ProviderProfile instance will do here
    register_provider_profile(ModelFamily.MISTRAL, custom)
    try:
        assert get_provider_profile(ModelFamily.MISTRAL) is custom
    finally:
        # Don't leak state into other tests — restore the fallback behavior.
        from velune.prompt_intelligence.profiles import _REGISTRY

        del _REGISTRY[ModelFamily.MISTRAL]
