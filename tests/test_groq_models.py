"""Regression test: GROQ_MODELS must not contain model IDs Groq has
decommissioned. The Council's role-mapper scores roles against this static
catalog and will assign a role to whatever scores best — a stale entry here
isn't cosmetic, it causes that agent's turn to fail with a live 400 from
Groq (confirmed for mixtral-8x7b-32768, gemma2-9b-it, and
llama-3.2-11b-vision-preview via a real GET /v1/models call, 2026-07).
"""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel
from velune.providers.adapters.groq import GROQ_MODELS

_DECOMMISSIONED = {
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
    "llama-3.2-11b-vision-preview",
}


def test_no_decommissioned_models_in_catalog():
    ids = {m.model_id for m in GROQ_MODELS}
    assert not (ids & _DECOMMISSIONED)


def test_catalog_is_not_empty():
    assert len(GROQ_MODELS) >= 2


def test_every_model_has_a_positive_context_length():
    for model in GROQ_MODELS:
        assert model.context_length > 0, model.model_id


def test_every_model_targets_groq_provider():
    for model in GROQ_MODELS:
        assert model.provider_id == "groq"


def test_replacement_models_have_advanced_coding_capability():
    # The two models that replaced the coding-capable roles' stale defaults
    # (mixtral was ADVANCED/ADVANCED for coding/reasoning) should be at least
    # as capable, not a silent downgrade.
    ids = {m.model_id: m for m in GROQ_MODELS}
    for model_id in ("openai/gpt-oss-120b", "qwen/qwen3-32b"):
        assert model_id in ids
        caps = ids[model_id].capabilities
        assert caps is not None
        assert caps.coding >= CapabilityLevel.ADVANCED
