"""Regression tests for RepositoryIntelligenceEngine bootstrap wiring.

Three separate bugs meant the "central coordinator for repository knowledge"
(change detection, git-state polling, knowledge-graph orphan cleanup) never
actually ran, even though the class itself was fully implemented:

1. ``INTELLIGENCE_MODULES`` (and ``KNOWLEDGE_MODULES``) lived in a
   ``module.py`` that no bootstrap aggregator ever imported — the engine and
   the knowledge graph were never even constructed.
2. Its ``SubsystemModule`` had ``lifecycle_key=None``, so even when
   constructed, ``LifecycleCoordinator`` never called ``.initialize()``.
3. Its factory looked up ``runtime.event_bus``, but the cognitive bus is
   registered under ``runtime.bus`` — the dependency could never resolve.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from velune.intelligence.subsystems import INTELLIGENCE_MODULES, _create_intelligence_engine
from velune.kernel.modules import load_background_modules


def test_intelligence_and_knowledge_modules_are_in_the_background_module_list():
    names = {m.name for m in load_background_modules()}
    assert "repository_intelligence_engine" in names
    assert "knowledge_graph" in names
    assert "knowledge_query" in names


def test_intelligence_module_is_lifecycle_managed():
    (module,) = INTELLIGENCE_MODULES
    assert module.lifecycle_key == "repository_intelligence"


def test_intelligence_module_depends_on_the_real_bus_key():
    (module,) = INTELLIGENCE_MODULES
    assert "runtime.bus" in module.dependencies
    assert "runtime.event_bus" not in module.dependencies


def _fake_env(*, watch_files=True, has_deps=True):
    env = MagicMock()
    env.config.workspace.watch_files = watch_files
    env.workspace = MagicMock()

    container_values = {}
    if has_deps:
        container_values["runtime.repository_cognition"] = MagicMock()
        container_values["runtime.knowledge_graph"] = MagicMock()
        container_values["runtime.bus"] = MagicMock()
    env.container.get.side_effect = lambda key, _v=container_values: _v.get(key)
    env.container.has.return_value = False  # no optional retrieval dep in this test
    return env


def test_factory_skips_when_watch_files_disabled():
    env = _fake_env(watch_files=False)
    assert _create_intelligence_engine(env) is None


def test_factory_skips_when_dependencies_missing():
    env = _fake_env(has_deps=False)
    assert _create_intelligence_engine(env) is None


def test_factory_constructs_engine_when_everything_is_available():
    env = _fake_env()
    engine = _create_intelligence_engine(env)
    assert engine is not None
    assert engine.__class__.__name__ == "RepositoryIntelligenceEngine"


async def test_lifecycle_startup_actually_initializes_the_engine():
    """End-to-end: LifecycleCoordinator.startup() must call .initialize()."""
    from velune.kernel.lifecycle import LifecycleCoordinator

    engine = MagicMock()
    engine.initialize = MagicMock(return_value=_awaitable(None))

    lifecycle = LifecycleCoordinator()
    lifecycle.register("repository_intelligence", engine)
    await lifecycle.startup()

    engine.initialize.assert_called_once()


def _awaitable(value):
    async def _coro():
        return value

    return _coro()
