"""Repository file changes must actually trigger memory upkeep.

Previously ``ThreeBrainCoordinator.clear_stale()`` and
``SemanticMemory.prune_low_vitality()`` both had zero callers anywhere in the
codebase: staleness was tracked and surfaced as an advisory note, but nothing
ever acted on it, so low-vitality semantic memory accumulated forever.
``MemoryLifecycleManager.retrieve()`` now reacts to a non-zero
``stale_file_count`` by scheduling a throttled background prune and resetting
the counter, per ``ThreeBrainCoordinator``'s own documented usage.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from velune.memory.lifecycle import MemoryLifecycleManager
from velune.memory.three_brain import ThreeBrainResult


def _manager(**overrides) -> MemoryLifecycleManager:
    defaults = {
        "working_tier": MagicMock(),
        "episodic_memory": MagicMock(),
        "semantic_memory": MagicMock(),
        "embedding_pipeline": MagicMock(),
        "lineage_tier": MagicMock(),
    }
    defaults.update(overrides)
    return MemoryLifecycleManager(**defaults)


async def test_stale_files_trigger_prune_and_clear_stale():
    coordinator = MagicMock()
    coordinator.query = AsyncMock(return_value=ThreeBrainResult(stale_file_count=3))
    coordinator.clear_stale = MagicMock()
    semantic = MagicMock()
    semantic.prune_low_vitality = AsyncMock(return_value=5)

    manager = _manager(three_brain=coordinator, semantic_memory=semantic)

    await manager.retrieve("q", "/workspace", budget=4000)
    # The prune is scheduled as a background task — give the loop a tick.
    await asyncio.sleep(0)

    semantic.prune_low_vitality.assert_awaited_once()
    coordinator.clear_stale.assert_called_once()


async def test_no_stale_files_does_not_prune_or_clear():
    coordinator = MagicMock()
    coordinator.query = AsyncMock(return_value=ThreeBrainResult(stale_file_count=0))
    coordinator.clear_stale = MagicMock()
    semantic = MagicMock()
    semantic.prune_low_vitality = AsyncMock(return_value=0)

    manager = _manager(three_brain=coordinator, semantic_memory=semantic)

    await manager.retrieve("q", "/workspace", budget=4000)
    await asyncio.sleep(0)

    semantic.prune_low_vitality.assert_not_awaited()
    coordinator.clear_stale.assert_not_called()


async def test_repeated_stale_signals_are_throttled():
    """A second stale signal within the throttle window must not re-prune."""
    coordinator = MagicMock()
    coordinator.query = AsyncMock(return_value=ThreeBrainResult(stale_file_count=1))
    coordinator.clear_stale = MagicMock()
    semantic = MagicMock()
    semantic.prune_low_vitality = AsyncMock(return_value=1)

    manager = _manager(three_brain=coordinator, semantic_memory=semantic)

    await manager.retrieve("q", "/workspace", budget=4000)
    await manager.retrieve("q", "/workspace", budget=4000)
    await asyncio.sleep(0)

    semantic.prune_low_vitality.assert_awaited_once()
    assert coordinator.clear_stale.call_count == 2  # still resets the counter every time
