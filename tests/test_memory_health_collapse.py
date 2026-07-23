"""Collapsing the three previously-disagreeing memory-health surfaces
(``velune memory stats``, ``velune doctor``, the REPL's ``/memory``) onto one
canonical source: ``MemoryLifecycleManager.health()`` — or, for ``doctor``
(a deliberately fast/standalone diagnostic with no shared runtime container),
``read_memory_health()``, which computes the exact same metrics from scratch.

Also covers two bugs found while wiring this: ``lancedb_size_mb`` read the
wrong attribute name and was silently always 0.0, and ``commands/memory.py``'s
new async stats path hung forever the first time around because it opened
the SQLite pool / LanceDB store via ``lifecycle.startup()`` but never called
``lifecycle.shutdown()`` — the background tasks that startup() spins up kept
the process alive indefinitely.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from velune.cli.commands.memory import _memory_stats_async
from velune.cli.display.memory_view import MemoryDisplayView
from velune.cli.handlers.memory import cmd_memory
from velune.memory.lifecycle import MemoryHealth, read_memory_health


def _health(**overrides) -> MemoryHealth:
    base = dict(
        working_memory_turns=3,
        episodic_sessions=2,
        semantic_indexed_count=17,
        embedding_queue_depth=1,
        lancedb_size_mb=4.2,
    )
    base.update(overrides)
    return MemoryHealth(**base)


# ---------------------------------------------------------------------------
# read_memory_health — the standalone path doctor uses
# ---------------------------------------------------------------------------


async def test_read_memory_health_cold_start_never_raises(tmp_path):
    health = await read_memory_health(tmp_path)
    assert health.working_memory_turns == 0
    assert health.episodic_sessions == 0
    assert health.semantic_indexed_count == 0
    assert health.lancedb_size_mb >= 0.0


async def test_read_memory_health_populates_lancedb_size_after_fix(tmp_path):
    """Regression: MemoryLifecycleManager.health() previously read
    semantic_memory._store._path (doesn't exist — the real attribute is
    _store_path), so lancedb_size_mb was silently always 0.0 no matter how
    much was actually indexed. A freshly created LanceDB directory already
    has nonzero bytes on disk once opened."""
    health = await read_memory_health(tmp_path)
    assert health.lancedb_size_mb > 0.0


# ---------------------------------------------------------------------------
# MemoryLifecycleManager.health() — semantic_indexed_count wiring
# ---------------------------------------------------------------------------


async def test_health_reads_semantic_indexed_count_from_store_table_size():
    from velune.memory.lifecycle import MemoryLifecycleManager

    store = MagicMock()
    store.table_size.return_value = 42
    semantic_memory = SimpleNamespace(_store=store)

    manager = MemoryLifecycleManager(
        working_tier=None,
        episodic_memory=None,
        semantic_memory=semantic_memory,
        embedding_pipeline=None,
        lineage_tier=None,
    )
    health = await manager.health()
    assert health.semantic_indexed_count == 42


# ---------------------------------------------------------------------------
# MemoryDisplayView.render_memory_health — shared table, no more "Qdrant"
# ---------------------------------------------------------------------------


def test_render_memory_health_never_mentions_qdrant():
    import io

    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    MemoryDisplayView(console).render_memory_health(_health())
    output = buf.getvalue()
    assert "Qdrant" not in output
    assert "LanceDB" in output
    assert "17" in output  # semantic_indexed_count
    assert "4.2" in output  # lancedb_size_mb


# ---------------------------------------------------------------------------
# REPL /memory — routes through the manager, no hardcoded placeholders
# ---------------------------------------------------------------------------


async def test_cmd_memory_renders_health_from_manager():
    printed: list[str] = []
    manager = MagicMock()
    manager.health = AsyncMock(return_value=_health())
    working = MagicMock()
    working.get_recent_turns.return_value = []

    repl = SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **k: printed.append(" ".join(map(str, a)))),
        container=SimpleNamespace(
            get=lambda key: {
                "runtime.working_memory": working,
                "runtime.memory_lifecycle": manager,
            }[key]
        ),
    )

    await cmd_memory(repl, "")

    manager.health.assert_awaited_once()
    assert not any("Qdrant" in line for line in printed)


async def test_cmd_memory_degrades_when_manager_missing():
    printed: list[str] = []
    working = MagicMock()
    working.get_recent_turns.return_value = []

    def _get(key):
        if key == "runtime.working_memory":
            return working
        raise KeyError(key)

    repl = SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **k: printed.append(" ".join(map(str, a)))),
        container=SimpleNamespace(get=_get),
    )

    await cmd_memory(repl, "")
    assert any("not available" in line for line in printed)


# ---------------------------------------------------------------------------
# doctor's _check_memory_health — same metrics, no direct file-size reimpl
# ---------------------------------------------------------------------------


def test_check_memory_health_uses_read_memory_health(monkeypatch):
    """_check_memory_health() imports read_memory_health locally on each
    call, so the patch has to target its source module — not a name cached
    on the doctor module — to actually take effect."""
    from velune.cli.commands import doctor as doctor_module

    async def fake_read(_workspace):
        return _health(episodic_sessions=5, semantic_indexed_count=9, lancedb_size_mb=1.5)

    monkeypatch.setattr("velune.memory.lifecycle.read_memory_health", fake_read)

    result = doctor_module._check_memory_health()
    assert result["status"] == "ok"
    assert "5 session(s)" in result["message"]
    assert "9 indexed" in result["message"]
    assert "1.5 MB" in result["message"]


def test_check_memory_health_degrades_on_error(monkeypatch):
    async def fake_read(_workspace):
        raise RuntimeError("disk full")

    monkeypatch.setattr("velune.memory.lifecycle.read_memory_health", fake_read)

    from velune.cli.commands import doctor as doctor_module

    result = doctor_module._check_memory_health()
    assert result["status"] == "warn"
    assert "disk full" in result["message"]


# ---------------------------------------------------------------------------
# commands/memory.py's stats path — regression test for the shutdown hang
# ---------------------------------------------------------------------------


async def test_memory_stats_async_always_shuts_down_lifecycle_even_on_error():
    """Regression: the first version of this function called
    lifecycle.startup() but never lifecycle.shutdown() — the background
    tasks startup() spins up (SQLite pool, LanceDB store, embedding
    pipeline) kept the process alive forever. manager.health() raising here
    must not skip shutdown()."""
    lifecycle = MagicMock()
    lifecycle.startup = AsyncMock()
    lifecycle.shutdown = AsyncMock()
    manager = MagicMock()
    manager.health = AsyncMock(side_effect=RuntimeError("boom"))

    cli_context = SimpleNamespace(
        config=SimpleNamespace(
            memory=SimpleNamespace(
                working_memory_ttl=3600,
                episodic_retention_days=30,
                semantic_threshold=0.85,
                graph_enabled=True,
            )
        ),
        workspace="/ws",
        json_mode=True,
        container=SimpleNamespace(
            get=lambda key: {
                "runtime.lifecycle": lifecycle,
                "runtime.memory_lifecycle": manager,
            }[key]
        ),
    )

    await _memory_stats_async(cli_context)

    lifecycle.startup.assert_awaited_once()
    lifecycle.shutdown.assert_awaited_once()


async def test_memory_stats_async_shuts_down_on_the_happy_path():
    lifecycle = MagicMock()
    lifecycle.startup = AsyncMock()
    lifecycle.shutdown = AsyncMock()
    manager = MagicMock()
    manager.health = AsyncMock(return_value=_health())

    cli_context = SimpleNamespace(
        config=SimpleNamespace(
            memory=SimpleNamespace(
                working_memory_ttl=3600,
                episodic_retention_days=30,
                semantic_threshold=0.85,
                graph_enabled=True,
            )
        ),
        workspace="/ws",
        json_mode=True,
        container=SimpleNamespace(
            get=lambda key: {
                "runtime.lifecycle": lifecycle,
                "runtime.memory_lifecycle": manager,
            }[key]
        ),
    )

    await _memory_stats_async(cli_context)

    lifecycle.startup.assert_awaited_once()
    lifecycle.shutdown.assert_awaited_once()
