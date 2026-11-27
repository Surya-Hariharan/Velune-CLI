"""Background work backs off when idle and never bursts: polling loops and startup probes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from velune.core.polling import IDLE_TICKS_BEFORE_BACKOFF, backoff_delay

# ── the pure backoff rule ─────────────────────────────────────────────────────


def test_base_rate_until_enough_idle_ticks():
    for ticks in range(IDLE_TICKS_BEFORE_BACKOFF):
        assert backoff_delay(3.0, ticks, 30.0) == 3.0


def test_doubles_after_the_idle_threshold_then_every_threshold():
    n = IDLE_TICKS_BEFORE_BACKOFF
    assert backoff_delay(3.0, n, 30.0) == 6.0
    assert backoff_delay(3.0, 2 * n, 30.0) == 12.0
    assert backoff_delay(3.0, 3 * n, 30.0) == 24.0


def test_never_exceeds_the_ceiling():
    assert backoff_delay(3.0, 10_000, 30.0) == 30.0
    assert backoff_delay(5.0, 10_000, 30.0) == 30.0


def test_a_base_above_the_ceiling_is_left_alone():
    assert backoff_delay(60.0, 10_000, 30.0) == 60.0  # test configs use huge bases on purpose


def test_delay_is_monotonic_non_decreasing():
    seq = [backoff_delay(3.0, t, 30.0) for t in range(200)]
    assert seq == sorted(seq)


# ── the engine's loops use it ─────────────────────────────────────────────────


class _FakeIndexer:
    """compute_delta returns empty until told otherwise."""

    def __init__(self, deltas):
        self._deltas = list(deltas)

    async def compute_delta(self):
        return self._deltas.pop(0) if self._deltas else _delta(empty=True)

    async def apply_delta(self, delta):
        return SimpleNamespace(last_commit_sha="abc")


def _delta(*, empty):
    return SimpleNamespace(
        is_empty=empty, to_add=[] if empty else ["a.py"], to_update=[], to_remove=[]
    )


def _engine(tmp_path: Path, base: float):
    from velune.intelligence.engine import RepositoryIntelligenceEngine

    eng = RepositoryIntelligenceEngine.__new__(RepositoryIntelligenceEngine)
    eng._workspace = tmp_path
    eng._state_path = tmp_path / "state.json"
    eng._change_poll = base
    eng._git_poll = base
    eng._running = True
    eng._last_change_at = 0.0
    eng._emitted = []

    async def emit(event):
        eng._emitted.append(event)

    eng._emit = emit
    eng._enqueue_downstream = lambda task: None
    return eng


async def _run_loop(eng, loop_name, monkeypatch, *, ticks):
    """Run a loop for *ticks* sleeps, recording each requested sleep without waiting."""
    from velune.intelligence import engine as engine_mod

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *a, **k):
        sleeps.append(delay)
        if len(sleeps) >= ticks:
            eng._running = False
        await real_sleep(0)

    monkeypatch.setattr(engine_mod.asyncio, "sleep", fake_sleep)
    await asyncio.wait_for(getattr(eng, loop_name)(), 5)
    return sleeps


async def test_change_loop_backs_off_while_nothing_changes(tmp_path, monkeypatch):
    from velune.intelligence import engine as engine_mod

    monkeypatch.setattr(engine_mod, "IncrementalIndexer", lambda *a, **k: _FakeIndexer([]))
    eng = _engine(tmp_path, base=3.0)
    sleeps = await _run_loop(eng, "_change_detection_loop", monkeypatch, ticks=30)
    assert sleeps[:5] == [3.0] * 5
    assert sleeps[-1] == engine_mod._IDLE_CHANGE_POLL_CEILING
    assert sleeps == sorted(sleeps)  # only ever slows down while idle


async def test_change_loop_snaps_back_to_the_base_rate_on_a_change(tmp_path, monkeypatch):
    from velune.intelligence import engine as engine_mod

    # 12 empty ticks (backed off), then a real change, then quiet again
    deltas = [_delta(empty=True)] * 12 + [_delta(empty=False)]
    monkeypatch.setattr(engine_mod, "IncrementalIndexer", lambda *a, **k: _FakeIndexer(deltas))
    eng = _engine(tmp_path, base=3.0)
    sleeps = await _run_loop(eng, "_change_detection_loop", monkeypatch, ticks=16)

    backed_off = max(sleeps[:13])
    assert backed_off > 3.0
    # after the change tick there is the cooldown sleep, then the very next poll is at base rate
    after_change = sleeps[13:]
    assert engine_mod._COOLDOWN_AFTER_CHANGE in after_change
    cooldown_at = after_change.index(engine_mod._COOLDOWN_AFTER_CHANGE)
    assert after_change[cooldown_at + 1] == 3.0


async def test_git_loop_backs_off_when_the_state_is_unchanged(tmp_path, monkeypatch):
    from velune.intelligence import engine as engine_mod

    eng = _engine(tmp_path, base=10.0)
    state = SimpleNamespace(branch="main", sha="1", uncommitted=0)
    eng._read_git_state = lambda: state
    eng._diff_git_state = lambda new: []  # nothing ever changes
    sleeps = await _run_loop(eng, "_git_state_loop", monkeypatch, ticks=40)
    assert sleeps[0] == 10.0
    assert sleeps[-1] == engine_mod._IDLE_GIT_POLL_CEILING


# ── startup model probes are bounded ──────────────────────────────────────────


async def test_startup_model_probes_never_exceed_the_concurrency_cap(tmp_path, monkeypatch):
    from tests.swarm_fakes import descriptor as make_model
    from velune.kernel import registry as kernel_registry
    from velune.models import probes
    from velune.models import registry as model_registry
    from velune.models.registry import ModelCapabilityRegistry

    monkeypatch.chdir(tmp_path)  # the probe cache path is cwd-relative
    provider_registry = SimpleNamespace(get=lambda pid: object())
    container = SimpleNamespace(get=lambda key: provider_registry)
    monkeypatch.setattr(kernel_registry, "get_container", lambda: container)

    in_flight = 0
    peak = 0

    async def ping(self, provider, model_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return False  # "not responding": no follow-up work, we only care about concurrency

    monkeypatch.setattr(probes.FastProbe, "ping", ping)
    models = [make_model(f"m{i}", "cloud", is_local=False) for i in range(20)]
    reg = ModelCapabilityRegistry.__new__(ModelCapabilityRegistry)
    await reg._probe(models)

    assert peak == model_registry._MAX_CONCURRENT_PROBES
    assert all(m.metadata.get("validated") is False for m in models)  # all 20 were probed


def test_the_probe_cap_is_a_small_number():
    from velune.models import registry as model_registry

    assert 1 <= model_registry._MAX_CONCURRENT_PROBES <= 8


@pytest.mark.parametrize("ticks", [0, 1, 4])
def test_no_backoff_inside_the_threshold(ticks):
    assert backoff_delay(5.0, ticks, 30.0) == 5.0
