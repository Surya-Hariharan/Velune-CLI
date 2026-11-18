"""Self-driven redraws back off as frames get expensive; input redraws are not slowed."""

from __future__ import annotations

import asyncio
import time

import pytest
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput

from velune.cli import fullscreen
from velune.cli.fullscreen import FullscreenREPLUI, adaptive_interval


def _make_ui() -> FullscreenREPLUI:
    return FullscreenREPLUI(
        status_state=None,
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda e: True,
        command_palette=None,
        output=DummyOutput(),
    )


# ── pure pacing rule ──────────────────────────────────────────────────────────


def test_cheap_frames_keep_the_base_rate():
    assert adaptive_interval(0.1, 0.0) == 0.1
    assert adaptive_interval(0.1, 0.005) == 0.1  # 3 x 5 ms is below the base


def test_expensive_frames_stretch_the_interval():
    assert adaptive_interval(0.1, 0.2) == pytest.approx(0.6)  # 3 x 200 ms


def test_the_interval_never_exceeds_the_ceiling():
    assert adaptive_interval(0.1, 5.0) == fullscreen._PACING_CEILING_S


def test_the_interval_is_never_below_the_base_even_with_a_big_base():
    assert adaptive_interval(2.0, 0.01) == 2.0  # base wins over the ceiling


def test_duty_cycle_is_bounded():
    # Animating costs frame_cost per tick; with the factor that is at most ~1/3 of a core.
    for cost in (0.05, 0.2, 0.33):
        interval = adaptive_interval(0.1, cost)
        assert cost / interval <= 1 / fullscreen._PACING_FACTOR + 1e-9


# ── wired into the UI ─────────────────────────────────────────────────────────


def test_a_fresh_ui_paces_at_the_base_rate():
    ui = _make_ui()
    assert ui._animation_interval(0.1) == 0.1


def test_render_hooks_measure_a_frame_and_smooth_it():
    ui = _make_ui()
    ui._on_before_render()
    time.sleep(0.05)
    ui._on_after_render()
    assert 0.01 < ui._render_cost_s < 0.05  # EMA: 0.3 x ~50 ms from a zero start
    first = ui._render_cost_s
    for _ in range(10):
        ui._on_before_render()
        time.sleep(0.05)
        ui._on_after_render()
    assert ui._render_cost_s > first  # converges toward the real cost


def test_the_application_is_actually_subscribed_to_the_render_events():
    ui = _make_ui()
    assert ui._app is not None
    # prompt_toolkit Events hold their handlers; ours must be among them
    assert ui._on_before_render in ui._app.before_render._handlers
    assert ui._on_after_render in ui._app.after_render._handlers


async def test_the_spinner_backs_off_when_frames_are_slow(monkeypatch):
    monkeypatch.delenv("VELUNE_REDUCED_MOTION", raising=False)
    monkeypatch.setattr(fullscreen.design, "reduced_motion_enabled", lambda: False)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(delay, *a, **k):
        sleeps.append(delay)
        await real_sleep(0)  # yield without waiting

    monkeypatch.setattr(fullscreen.asyncio, "sleep", spy_sleep)

    ui = _make_ui()
    ui._render_cost_s = 0.3  # a heavy transcript: 300 ms per frame
    ui.begin_assistant("thinking")
    for _ in range(5):
        await real_sleep(0)
    ui.finish_assistant()

    assert sleeps, "the thinking animation never ticked"
    assert min(sleeps) == pytest.approx(0.9)  # 3 x 300 ms, not the 0.1 s base


async def test_the_spinner_runs_at_the_base_rate_when_frames_are_cheap(monkeypatch):
    monkeypatch.setattr(fullscreen.design, "reduced_motion_enabled", lambda: False)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(delay, *a, **k):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(fullscreen.asyncio, "sleep", spy_sleep)
    ui = _make_ui()
    ui.begin_assistant("thinking")
    for _ in range(5):
        await real_sleep(0)
    ui.finish_assistant()
    assert sleeps and set(sleeps) == {fullscreen._THINKING_TICK_S}


def test_streamed_markdown_refresh_is_throttled_harder_when_frames_are_slow(monkeypatch):
    ui = _make_ui()
    renders: list[int] = []
    monkeypatch.setattr(ui, "_render_stream_markdown", lambda final: renders.append(1))
    ui._stream_start = 0
    ui._render_cost_s = 0.3  # 0.9 s between markdown refreshes

    ui._last_stream_render = time.perf_counter() - 0.5  # 0.5 s ago: > 80 ms base, < 0.9 s paced
    ui.update_assistant("chunk")
    assert renders == []  # held back by the adaptive throttle

    ui._last_stream_render = time.perf_counter() - 1.5
    ui.update_assistant("chunk two")
    assert renders == [1]


def test_the_final_update_is_never_throttled(monkeypatch):
    ui = _make_ui()
    renders: list[int] = []
    monkeypatch.setattr(ui, "_render_stream_markdown", lambda final: renders.append(1))
    ui._stream_start = 0
    ui._render_cost_s = 0.3
    ui._last_stream_render = time.perf_counter()  # just rendered
    ui.update_assistant("done", final=True)
    assert renders == [1]
