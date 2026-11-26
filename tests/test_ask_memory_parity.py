"""C3 regression: `velune ask` must retrieve persistent memory the same way
the REPL does, instead of going straight from a fresh AST snapshot to the
council with no memory retrieval at all."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.memory.lifecycle import RetrievedContext, RetrievedResult


class _Lifecycle:
    async def startup(self):
        pass

    async def shutdown(self):
        pass


class _Registry:
    async def refresh(self):
        pass

    def list_all(self):
        return [SimpleNamespace(provider_id="ollama", model_id="m")]


class _Orchestrator:
    """Stands in for the council: records the context ``velune ask`` hands it."""

    def __init__(self, monkeypatch):
        self.received_repo_context: str | None = None
        monkeypatch.setattr("velune.swarm.setup.run_headless", self.run)

    async def run(self, container, task, **kwargs):
        self.received_repo_context = kwargs["context"]
        return SimpleNamespace(
            ok=True,
            status="ok",
            content="ok",
            error="",
            run_id="r1",
            results=[],
            plan=SimpleNamespace(
                skill="research", permission="answer-only", output="text", notes=[]
            ),
        )


class _Container:
    def __init__(self, memory_manager, orchestrator):
        self._values = {
            "runtime.lifecycle": _Lifecycle(),
            "runtime.model_registry": _Registry(),
            "runtime.repository_cognition": SimpleNamespace(index=lambda: None),
            "runtime.workspace": None,
            "runtime.memory_lifecycle": memory_manager,
        }

    def get(self, key):
        return self._values[key]


@pytest.mark.asyncio
async def test_ask_merges_memory_lifecycle_retrieval_into_council_context(tmp_path, monkeypatch):
    monkeypatch.setattr("velune.providers.keystore.list_invalid_providers", lambda: [])
    monkeypatch.setattr("velune.providers.keystore.is_ollama_live", lambda timeout=0.25: True)

    manager = MagicMock()
    manager.retrieve = AsyncMock(
        return_value=RetrievedContext(
            results=[
                RetrievedResult(
                    content="prior decision: use SQLite for episodic memory",
                    source_type="semantic",
                    trust_score=0.8,
                ),
            ]
        )
    )
    orchestrator = _Orchestrator(monkeypatch)
    container = _Container(manager, orchestrator)
    ctx = SimpleNamespace(container=container, json_mode=True, workspace=tmp_path)

    from velune.cli.commands import ask as ask_mod

    await ask_mod._ask_with_runtime(ctx, "what did we decide about storage?")

    manager.retrieve.assert_awaited_once()
    assert orchestrator.received_repo_context is not None
    assert "Relevant memory" in orchestrator.received_repo_context
    assert "SQLite for episodic memory" in orchestrator.received_repo_context


@pytest.mark.asyncio
async def test_ask_degrades_silently_when_memory_lifecycle_missing(tmp_path, monkeypatch):
    """No runtime.memory_lifecycle registered — ask must still complete, just
    without the memory section, not crash."""
    monkeypatch.setattr("velune.providers.keystore.list_invalid_providers", lambda: [])
    monkeypatch.setattr("velune.providers.keystore.is_ollama_live", lambda timeout=0.25: True)

    orchestrator = _Orchestrator(monkeypatch)
    container = _Container(memory_manager=None, orchestrator=orchestrator)
    ctx = SimpleNamespace(container=container, json_mode=True, workspace=tmp_path)

    from velune.cli.commands import ask as ask_mod

    await ask_mod._ask_with_runtime(ctx, "hello")

    assert orchestrator.received_repo_context is not None
    assert "Relevant memory" not in orchestrator.received_repo_context
