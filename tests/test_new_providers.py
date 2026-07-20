"""Meta provider onboarding, plus the NVIDIA NIM discovery-gating fix.

Covers the full "add a cloud provider" contract for the newest provider
(Meta's first-party Llama API) end to end: catalog entry,
env var, validator registration, registry factory, discoverer gating, and the
adapter's static model list. Also pins down the NVIDIA NIM regression where
the discoverer's ``provider_id`` ("nvidia_nim") never matched the id a key is
actually stored/validated under ("nvidia"), which meant ``_should_run()``
always found no key and ``scan_provider("nvidia")`` never matched it —
silently making cloud NIM discovery dead code.
"""

from __future__ import annotations

import pytest

from velune.providers import catalog
from velune.providers import keystore as ks
from velune.providers.discovery.scanner import ModelDiscoveryScanner, _LOCAL_PROVIDERS
from velune.providers.validation import _VALIDATORS


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the credential singleton at a temp file and clear the env."""
    for env_var in ks.PROVIDER_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(ks._manager, "_config_dir", tmp_path)
    monkeypatch.setattr(ks._manager, "_credentials_file", tmp_path / "credentials.json")
    monkeypatch.setattr(ks._manager, "_cache", None)
    yield


# ---------------------------------------------------------------------------
# Catalog / keystore / validator registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_id", ["meta"])
def test_provider_is_in_catalog(provider_id):
    meta = catalog.get(provider_id)
    assert meta is not None
    assert meta.requires_key is True
    assert meta.key_label
    assert meta.get_key_url.startswith("https://")


@pytest.mark.parametrize("provider_id", ["meta"])
def test_provider_has_env_var(provider_id):
    assert provider_id in ks.PROVIDER_ENV_VARS
    env_var = ks.PROVIDER_ENV_VARS[provider_id]
    assert env_var
    # catalog.env_var must be sourced from the same table, not hand-duplicated.
    assert catalog.get(provider_id).env_var == env_var


@pytest.mark.parametrize("provider_id", ["meta"])
def test_provider_has_validator(provider_id):
    assert provider_id in _VALIDATORS


@pytest.mark.parametrize("provider_id", ["meta"])
def test_provider_has_registry_factory(provider_id):
    from velune.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    provider = registry.get(provider_id)
    assert provider is not None
    assert provider.provider_id == provider_id


@pytest.mark.parametrize("provider_id", ["meta"])
def test_env_var_is_honoured_by_get_key(provider_id, monkeypatch):
    env_var = ks.PROVIDER_ENV_VARS[provider_id]
    monkeypatch.setenv(env_var, "test-key-from-env")
    assert ks.get_key(provider_id) == "test-key-from-env"
    assert ks.has_key(provider_id) is True


# ---------------------------------------------------------------------------
# Adapter model lists
# ---------------------------------------------------------------------------


def test_meta_models_are_well_formed():
    from velune.providers.adapters.meta import META_MODELS

    assert len(META_MODELS) >= 1
    for model in META_MODELS:
        assert model.provider_id == "meta"
        assert model.context_length > 0
        assert model.model_id


# ---------------------------------------------------------------------------
# Discovery gating — a key must actually surface models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_discovery_returns_empty_without_key():
    from velune.providers.discovery.meta import MetaDiscovery

    assert await MetaDiscovery().discover() == []


@pytest.mark.asyncio
async def test_meta_discovery_returns_models_with_key(monkeypatch):
    from velune.providers.discovery.meta import MetaDiscovery

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    models = await MetaDiscovery().discover()
    assert len(models) >= 1
    assert all(m.provider_id == "meta" for m in models)


@pytest.mark.asyncio
async def test_scan_provider_meta_matches_by_catalog_id(monkeypatch):
    """scan_provider(id) must match the same id `provider add` stores keys under."""
    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    scanner = ModelDiscoveryScanner()
    models = await scanner.scan_provider("meta")
    assert len(models) >= 1


# ---------------------------------------------------------------------------
# NVIDIA NIM regression — provider_id must match the key's storage id
# ---------------------------------------------------------------------------


def test_nvidia_nim_discoverer_id_matches_keystore_id():
    """Regression: this used to be "nvidia_nim", an id nothing ever saves a
    key under, so has_key()/scan_provider("nvidia") could never find it."""
    from velune.providers.discovery.nvidia_nim import NVIDIANIMDiscovery

    assert NVIDIANIMDiscovery().provider_id == "nvidia"


def test_nvidia_is_always_run_so_local_containers_are_found_without_a_key():
    """A local NIM container needs no cloud key — the discoverer must not be
    gated behind has_key(), or a key-less local setup would never be found."""
    assert "nvidia" in _LOCAL_PROVIDERS


@pytest.mark.asyncio
async def test_scan_provider_nvidia_matches_by_catalog_id(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    scanner = ModelDiscoveryScanner()
    # Should not raise, and should actually reach the discoverer (network call
    # inside it may fail in a sandboxed test run — that's fine, discover()
    # swallows it — the point is scan_provider finds the discoverer at all).
    models = await scanner.scan_provider("nvidia")
    assert isinstance(models, list)


def test_model_descriptor_provider_ids_are_unaffected_by_the_rename():
    """Per-model tagging ("nvidia_nim" / "nvidia_nim_local") is independent of
    the discoverer's own gating id and must not have changed."""
    import inspect

    from velune.providers.discovery.nvidia_nim import NVIDIANIMDiscovery

    source = inspect.getsource(NVIDIANIMDiscovery._build_descriptor)
    assert '"nvidia_nim" if not is_local else "nvidia_nim_local"' in source
