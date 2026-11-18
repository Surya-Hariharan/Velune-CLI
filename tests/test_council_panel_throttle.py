"""The council streaming panel is rebuilt on a clock, not once per token."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from tests.council_fakes import make_model
from velune.cognition.council import base as council_base
from velune.cognition.council.reviewer import ReviewerAgent

MESSAGES = [{"role": "user", "content": "judge this"}]
N_CHUNKS = 400


class _FastProvider:
    provider_id = "fake"

    def get_capabilities(self):
        return SimpleNamespace(supports_streaming=True)

    async def stream(self, request):
        for i in range(N_CHUNKS):
            yield SimpleNamespace(content=f"tok{i} ")

    async def infer(self, request):  # pragma: no cover
        raise AssertionError("should stream")


async def _run(monkeypatch, interval):
    builds = []
    real = council_base.__dict__.get("Panel")  # Panel is imported lazily; count via CustomMarkdown
    from velune.cli import rendering

    original = rendering.CustomMarkdown

    class Counting(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            builds.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(rendering, "CustomMarkdown", Counting)
    monkeypatch.setattr(council_base, "_PANEL_UPDATE_INTERVAL_S", interval)
    sink = StringIO()
    agent = ReviewerAgent(make_model(), _FastProvider())
    agent.live_lock = asyncio.Lock()
    agent.live_console = Console(file=sink, force_terminal=True, width=80)
    text = await agent.deliberate(MESSAGES)
    del real
    return text, len(builds), sink.getvalue()


async def test_panel_is_not_rebuilt_per_token(monkeypatch):
    text, builds, _ = await _run(monkeypatch, interval=3600.0)
    assert text.startswith("tok0 ") and text.rstrip().endswith(f"tok{N_CHUNKS - 1}")
    # one paint for the first chunk + one final paint; never one per token
    assert builds <= 3, builds


async def test_every_chunk_still_paints_when_the_interval_is_zero(monkeypatch):
    _, builds, _ = await _run(monkeypatch, interval=0.0)
    assert builds >= N_CHUNKS  # proves the counter measures per-token rebuilds


async def test_final_text_is_painted_even_if_the_last_chunks_were_throttled(monkeypatch):
    text, _, out = await _run(monkeypatch, interval=3600.0)
    assert f"tok{N_CHUNKS - 1}" in out
