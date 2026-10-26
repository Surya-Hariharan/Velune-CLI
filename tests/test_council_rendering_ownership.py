"""The council never opens a rendering region of its own.

The full-screen REPL owns the terminal. A council agent that builds its own
``Console()`` and ``Live`` on the real stdout paints over the composer and the
transcript (audit finding TUI-01), and the REPL handler's own ``Live`` on the
transcript sink either draws nothing or fights the composer. These tests pin the
rule at the two places that used to break it: the agent and the REPL handler.
The end-to-end proof, on real terminal bytes, is ``tests/tui/test_council_journeys.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from tests.council_fakes import make_model
from tests.test_council_phase0_characterization import _state, _StubOrchestrator
from velune.cognition.council.reviewer import ReviewerAgent
from velune.orchestration.schemas import ExecutionStatus

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


def _repl_with(orchestrator, *, cancelled=False):
    from velune.cli.modes import ModeManager

    buffer = StringIO()

    @contextlib.asynccontextmanager
    async def foreground():
        yield

    services = {"runtime.council_orchestrator": orchestrator}
    repl = SimpleNamespace(
        console=Console(file=buffer, width=100, color_system=None),
        container=SimpleNamespace(get=lambda key: services[key], has=lambda key: key in services),
        _interrupts=SimpleNamespace(
            foreground=foreground, consume_user_cancelled=lambda: cancelled
        ),
        _conversation=[],
        _mode_manager=ModeManager(),
        _job_registry=None,
    )
    repl.output = buffer
    return repl


class _PhasedOrchestrator(_StubOrchestrator):
    async def stream(self, prompt, *args, **kwargs):
        for phase, message in (
            ("planner", "Decomposing task..."),
            ("coder", "Designing code implementation..."),
            ("coder", "Self-consistency vote"),
            ("reviewer", "Running review and critique..."),
            ("synthesis", "Synthesizing final summary..."),
        ):
            yield SimpleNamespace(run_id="r1", phase=phase, message=message, elapsed_ms=12.0)


async def test_handler_reports_each_phase_without_a_live_region(no_live):
    from velune.cli.handlers.council import execute_council_task

    repl = _repl_with(_PhasedOrchestrator(_state()))
    await execute_council_task(repl, "do it", force_tier=None)

    out = repl.output.getvalue()
    assert no_live == []
    for phase in ("Planner", "Coder", "Reviewer", "Synthesis"):
        assert f"▸ {phase}…" in out  # immediate, append-only progress
        assert f"{phase} Phase" in out  # and the finished-phase panel
    assert out.count("▸ Coder…") == 1  # two milestones, one phase header
    assert "Council Result" in out
    assert repl._conversation[-1]["content"] == "the answer"


async def test_handler_does_not_scan_the_workspace_on_the_event_loop(no_live):
    """The old pre-scan called the synchronous indexer inline and threw the result away."""
    from velune.cli.handlers.council import execute_council_task

    repl = _repl_with(_PhasedOrchestrator(_state()))
    asked: list[str] = []
    original = repl.container.get

    def spy(key):
        asked.append(key)
        return original(key)

    repl.container = SimpleNamespace(get=spy, has=repl.container.has)
    await execute_council_task(repl, "do it", force_tier=None)
    assert "runtime.repository_cognition" not in asked


class _CancelledOrchestrator(_StubOrchestrator):
    async def stream(self, prompt, *args, **kwargs):
        yield SimpleNamespace(run_id="r1", phase="planner", message="start", elapsed_ms=None)
        raise asyncio.CancelledError


async def test_handler_cancellation_prints_a_notice_and_returns_control(no_live):
    from velune.cli.handlers.council import execute_council_task

    repl = _repl_with(_CancelledOrchestrator(_state()), cancelled=True)
    await asyncio.wait_for(execute_council_task(repl, "do it", force_tier=None), 5)
    out = repl.output.getvalue()
    assert "Council run interrupted." in out
    assert repl._conversation == []  # an interrupted run leaves no half-answer behind


class _ExplodingOrchestrator(_StubOrchestrator):
    async def stream(self, prompt, *args, **kwargs):
        yield SimpleNamespace(run_id="r1", phase="coder", message="working", elapsed_ms=None)
        raise RuntimeError("provider exploded")


async def test_handler_error_is_shown_and_leaves_state_clean(no_live):
    from velune.cli.handlers.council import execute_council_task

    repl = _repl_with(_ExplodingOrchestrator(_state()))
    await execute_council_task(repl, "do it", force_tier=None)
    assert "provider exploded" in repl.output.getvalue()
    assert repl._conversation == []


async def test_handler_failed_run_shows_failure_panel_not_a_result(no_live):
    from velune.cli.handlers.council import execute_council_task

    failed = _state(ExecutionStatus.FAILED, output="Execution failed: boom", error="boom")
    repl = _repl_with(_PhasedOrchestrator(failed))
    await execute_council_task(repl, "do it", force_tier=None)
    out = repl.output.getvalue()
    assert "Council Failed" in out and "Council Result" not in out
