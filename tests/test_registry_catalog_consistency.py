"""ProviderRegistry and providers/catalog.py must never silently drift apart.

Before Phase 0 of the provider-management rework, three tables listed
provider ids independently: registry.py's own hardcoded factory list,
catalog.py's display metadata, and cli/commands/providers.py's separate
``_PROVIDER_META``. ``llamacpp``/``openai-compat`` existed in the registry
but neither metadata table, so they never showed up in ``/providers`` or
``velune provider list``. ``ProviderRegistry._assert_matches_catalog`` turns
any future version of that drift into a loud startup failure instead of a
silent display gap.
"""

from __future__ import annotations

import pytest

from velune.providers import catalog
from velune.providers.registry import ProviderRegistry


def test_real_registry_matches_catalog():
    """The actual, current registry must construct without raising."""
    registry = ProviderRegistry()
    registered = set(registry.list_providers())
    cataloged = {p.id for p in catalog.list_providers_alphabetical()}
    assert registered == cataloged


def test_llamacpp_and_openai_compat_are_in_the_catalog():
    """These two used to exist only in registry.py's hardcoded list."""
    assert catalog.get("llamacpp") is not None
    assert catalog.get("openai-compat") is not None
    assert catalog.get("llamacpp").requires_key is False
    assert catalog.get("openai-compat").requires_key is False


def test_mismatch_between_registry_and_catalog_raises(monkeypatch):
    """A provider registered but missing from the catalog must fail loudly
    at construction, not silently vanish from the provider list UI."""

    class _DriftedRegistry(ProviderRegistry):
        def _register_default_providers(self) -> None:
            self.register_factory("totally-unlisted-provider", lambda: None)
            self._assert_matches_catalog()

    with pytest.raises(RuntimeError, match="drifted apart"):
        _DriftedRegistry()
