"""The council never opens a rendering region of its own.

The full-screen REPL owns the terminal. A council agent that builds its own
``Console()`` and ``Live`` on the real stdout paints over the composer and the
transcript (audit finding TUI-01). These tests pin the rule for the old council
agents; the swarm's REPL view is covered in ``tests/test_swarm_entry.py``.
"""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from tests.council_fakes import make_model
from velune.cognition.council.reviewer import ReviewerAgent

MESSAGES = [{"role": "user", "content": "judge this"}]


class _StreamingProvider:
    """A provider that advertises streaming, as every real cloud provider does."""

    provider_id = "fake"

    def __init__(self) -> None:
        self.streamed = 0

    def get_capabilities(self):
        return SimpleNamespace(supports_streaming=True)

    async def stream(self, request):
        self.streamed += 1
        for piece in ("hello ", "from ", "the stream"):
            yield SimpleNamespace(content=piece)

    async def infer(self, request):  # pragma: no cover - must not be used here
        raise AssertionError("a streaming-capable provider should be streamed")


@pytest.fixture
def no_live(monkeypatch):
    """Fail loudly if anything builds a Rich ``Live`` during the test."""
    built: list[object] = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            built.append(args)
            raise AssertionError("a Rich Live region was created")

    monkeypatch.setattr("rich.live.Live", _Forbidden)
    return built


class _FakeTTY(StringIO):
    """stdout as a real terminal presents it -- the condition the old code keyed on."""

    def isatty(self) -> bool:
        return True


async def test_agent_without_an_injected_console_draws_nothing(no_live, monkeypatch):
    tty = _FakeTTY()
    monkeypatch.setattr("sys.stdout", tty)
    provider = _StreamingProvider()
    agent = ReviewerAgent(make_model(), provider)
    agent.live_lock = asyncio.Lock()  # the orchestrator always supplies one

    text = await agent.deliberate(MESSAGES)

    assert text == "hello from the stream"
    assert provider.streamed == 1  # still streamed, just not drawn
    assert no_live == []
    assert tty.getvalue() == ""  # nothing was written to the terminal


async def test_agent_only_ever_draws_on_the_console_it_was_given():
    sink = StringIO()
    console = Console(file=sink, force_terminal=True, width=80)
    agent = ReviewerAgent(make_model(), _StreamingProvider())
    agent.live_lock = asyncio.Lock()
    agent.live_console = console

    text = await agent.deliberate(MESSAGES)

    assert text == "hello from the stream"
    assert "Agent Deliberating" in sink.getvalue()  # the opt-in path still works
