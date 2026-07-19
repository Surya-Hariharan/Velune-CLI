"""ContextCompactor's utilization trigger must scale with the hardware profile.

Before this fix, MemoryLifecycleManager always constructed ContextCompactor
with max_context_tokens=100000 hardcoded, regardless of hardware. The
compactor's 75%-utilization trigger (see ContextCompactor.should_compact) was
therefore only reachable at 75,000 tokens of raw working-memory text — on a
LOW_RESOURCE machine (RuntimeProfile.max_context_tokens=4096) that made the
token-based trigger essentially unreachable, leaving compaction to fire only
via the >30-turn fallback and letting far more raw conversation text
accumulate in RAM than the profile intends.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from velune.hardware.profiles import RuntimeProfileName, get_profile
from velune.kernel.registry import ServiceContainer
from velune.memory.lifecycle import MemoryLifecycleManager


def _make_manager() -> MemoryLifecycleManager:
    return MemoryLifecycleManager(
        working_tier=MagicMock(),
        episodic_memory=MagicMock(),
        semantic_memory=MagicMock(),
        embedding_pipeline=MagicMock(),
        lineage_tier=MagicMock(),
    )


def test_uses_low_resource_profile_budget_when_registered(monkeypatch):
    container = ServiceContainer()
    container.register_instance("runtime.profile", get_profile(RuntimeProfileName.LOW_RESOURCE))
    monkeypatch.setattr("velune.kernel.registry._container", container)
    monkeypatch.setattr("velune.kernel.registry.get_container", lambda: container)

    manager = _make_manager()
    assert manager._compaction_token_budget() == 4096


def test_uses_maximum_profile_budget_when_registered(monkeypatch):
    container = ServiceContainer()
    container.register_instance("runtime.profile", get_profile(RuntimeProfileName.MAXIMUM))
    monkeypatch.setattr("velune.kernel.registry._container", container)
    monkeypatch.setattr("velune.kernel.registry.get_container", lambda: container)

    manager = _make_manager()
    assert manager._compaction_token_budget() == 65536


def test_falls_back_to_100000_when_no_profile_registered(monkeypatch):
    container = ServiceContainer()  # no "runtime.profile" registered
    monkeypatch.setattr("velune.kernel.registry._container", container)
    monkeypatch.setattr("velune.kernel.registry.get_container", lambda: container)

    manager = _make_manager()
    assert manager._compaction_token_budget() == 100000


def test_falls_back_to_100000_on_container_error(monkeypatch):
    def _broken():
        raise RuntimeError("container unavailable")

    monkeypatch.setattr("velune.kernel.registry.get_container", _broken)

    manager = _make_manager()
    assert manager._compaction_token_budget() == 100000
