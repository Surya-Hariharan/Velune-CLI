"""Model -> provider resolution must be canonical and never disagree with
itself: the model registry, the provider adapter actually used for
inference, and the health monitor's own provider id must all point at the
same provider for a given model.

Regression coverage: a status-bar screenshot showed the active model as
`llama-3.3-70b-versatile · groq` while a health warning simultaneously read
"Provider llamacpp unavailable" — investigation traced that specific warning
to an unrelated, always-broken-by-design provider (see
test_llamacpp_false_alert.py), not a real model->provider mix-up. This file
pins the actual mapping so a future regression of *that* kind (a model
resolving to the wrong adapter) is caught directly, rather than relying on
absence-of-a-symptom.
"""

from __future__ import annotations

from velune.providers.adapters.groq import GROQ_MODELS, GroqProvider
from velune.providers.catalog import get as catalog_get


def test_every_groq_model_declares_groq_as_its_provider():
    for model in GROQ_MODELS:
        assert model.provider_id == "groq"


def test_llama_3_3_70b_resolves_to_the_groq_adapter():
    model = next(m for m in GROQ_MODELS if m.model_id == "llama-3.3-70b-versatile")
    assert model.provider_id == "groq"

    provider = GroqProvider(api_key="gsk_test")
    assert provider.provider_id == "groq"
    assert provider.provider_id == model.provider_id


def test_groq_provider_id_matches_its_own_catalog_entry():
    """The health monitor, the credential store, and the adapter must all
    key on the exact same provider id — a mismatch here is exactly the shape
    of bug that would let a healthy Groq connection get reported under a
    different provider's health state."""
    provider = GroqProvider(api_key="gsk_test")
    meta = catalog_get(provider.provider_id)

    assert meta is not None
    assert meta.id == "groq"
    assert meta.display_name == "Groq"
    assert meta.id == provider.provider_id


def test_groq_is_never_aliased_to_a_local_provider_id():
    """Canonical identity check: nothing conflates "groq" with any of the
    local/self-hosted provider ids it must never be confused with."""
    provider = GroqProvider(api_key="gsk_test")
    assert provider.provider_id not in ("llamacpp", "llama.cpp", "local", "ollama", "groq-cloud")


def test_groq_provider_registry_id_is_unique_in_the_catalog():
    from velune.providers import catalog

    ids = [p.id for p in catalog.list_providers_alphabetical()]
    assert ids.count("groq") == 1
    assert len(ids) == len(set(ids)), "no provider id may be registered twice"
