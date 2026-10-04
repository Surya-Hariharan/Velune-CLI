"""Provider capability/preference registry, keyed by ``ModelFamily``.

Adding a new provider is one :func:`register_provider_profile` call — see
the Prompt Intelligence design notes §17. Built on top of the existing
:func:`velune.models.family.detect_family`; this module does not add a
second family detector.
"""

from __future__ import annotations

from velune.models.family import ModelFamily
from velune.prompt_intelligence.ir import ProviderProfile
from velune.prompt_intelligence.profiles.anthropic import ANTHROPIC_PROFILE
from velune.prompt_intelligence.profiles.gemini import GEMINI_PROFILE
from velune.prompt_intelligence.profiles.generic import GENERIC_PROFILE
from velune.prompt_intelligence.profiles.openai import OPENAI_PROFILE

_REGISTRY: dict[ModelFamily, ProviderProfile] = {
    ModelFamily.CLAUDE: ANTHROPIC_PROFILE,
    ModelFamily.GPT: OPENAI_PROFILE,
    ModelFamily.GEMINI: GEMINI_PROFILE,
}


def get_provider_profile(family: ModelFamily) -> ProviderProfile:
    """Resolve *family* to its profile, falling back to :data:`GENERIC_PROFILE`
    for any unregistered family — never fail a turn over a missing profile (§14)."""
    return _REGISTRY.get(family, GENERIC_PROFILE)


def register_provider_profile(family: ModelFamily, profile: ProviderProfile) -> None:
    """Extensibility hook — add a new family without touching the compiler."""
    _REGISTRY[family] = profile


__all__ = [
    "get_provider_profile",
    "register_provider_profile",
    "ANTHROPIC_PROFILE",
    "OPENAI_PROFILE",
    "GEMINI_PROFILE",
    "GENERIC_PROFILE",
]
