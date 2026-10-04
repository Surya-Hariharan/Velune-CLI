"""Regression test: GROQ_MODELS must not contain model IDs Groq has
decommissioned. The Council's role-mapper scores roles against this static
catalog and will assign a role to whatever scores best — a stale entry here
isn't cosmetic, it causes that agent's turn to fail with a live error from
Groq (confirmed for mixtral-8x7b-32768, gemma2-9b-it, and
llama-3.2-11b-vision-preview via a real GET /v1/models call, 2026-07;
qwen/qwen3-32b via console.groq.com/docs/deprecations, 2026-08 — Groq
deprecated it 2026-07-17 with openai/gpt-oss-120b as the stated successor,
and selecting it produced a live HTTP 404 on /chat/completions).
"""

from __future__ import annotations

from velune.core.types.model import CapabilityLevel
from velune.providers.adapters.groq import GROQ_MODELS

_DECOMMISSIONED = {
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
    "llama-3.2-11b-vision-preview",
    "qwen/qwen3-32b",
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
    # The model that replaced the coding-capable roles' stale defaults
    # (mixtral was ADVANCED/ADVANCED for coding/reasoning) should be at least
    # as capable, not a silent downgrade.
    ids = {m.model_id: m for m in GROQ_MODELS}
    for model_id in ("openai/gpt-oss-120b",):
        assert model_id in ids
        caps = ids[model_id].capabilities
        assert caps is not None
        assert caps.coding >= CapabilityLevel.ADVANCED


def test_reconcile_drops_models_groq_no_longer_serves():
    from velune.providers.adapters.groq import reconcile_with_live

    result = {
        m.model_id
        for m in reconcile_with_live({"openai/gpt-oss-120b": 131072, "openai/gpt-oss-20b": 131072})
    }
    assert "llama-3.3-70b-versatile" not in result
    assert result == {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}


def test_reconcile_skips_non_chat_models_and_adds_new_chat_models():
    from velune.providers.adapters.groq import reconcile_with_live

    live = {
        "openai/gpt-oss-120b": 131072,
        "qwen/qwen3.8-27b": 131072,
        "whisper-large-v3": 448,
        "meta-llama/llama-prompt-guard-2-22m": 512,
    }
    result = {m.model_id for m in reconcile_with_live(live)}
    assert result == {"openai/gpt-oss-120b", "qwen/qwen3.8-27b"}


def test_reconcile_falls_back_to_static_catalog_when_offline():
    from velune.providers.adapters.groq import reconcile_with_live

    assert {m.model_id for m in reconcile_with_live(None)} == {m.model_id for m in GROQ_MODELS}


def test_reconcile_uses_the_context_window_groq_reports():
    from velune.providers.adapters.groq import reconcile_with_live

    models = {
        m.model_id: m for m in reconcile_with_live({"allam-2-7b": 4096, "mystery-model": None})
    }
    assert models["allam-2-7b"].context_length == 4096
    assert models["mystery-model"].context_length == 8192
