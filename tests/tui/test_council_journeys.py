"""`/council` on the real CLI in a pseudo-terminal.

The council used to paint independent Rich ``Live`` panels onto the real stdout
underneath the full-screen app, overwriting the transcript and the composer row
(audit finding TUI-01). These journeys drive the actual program against the local
fake backend and assert on the emulated screen *and* the raw byte stream.

"A later prompt was processed" is always proven with a marker only that reply
contains (``give me MULTILINE`` -> ``Done.``), never with text a council panel could
have left behind (every council seat answers ``FAKE-REPLY-OK``).
"""

from __future__ import annotations

import time

import pytest

from . import flows
from .fake_backend import FakeBackend

TASK = "/council where is tax computed in this repo"
LIVE_PANEL_TITLE = "Agent Deliberating"


@pytest.fixture
def council_repl(launch, tmp_path, backend: FakeBackend):
    work = tmp_path / "work"
    (work / "pkg").mkdir(parents=True)
    (work / "pkg" / "util.py").write_text("def tax(x):\n    return x * 0.2\n", encoding="utf-8")
    (work / "pkg" / "billing.py").write_text(
        "from pkg.util import tax\n\n\ndef total(x):\n    return x + tax(x)\n", encoding="utf-8"
    )
    s = launch()
    flows.skip_onboarding(s)
    flows.connect(s)
    s.wait_for("OpenAI connected.", 30)
    flows.idle(s)
    backend.chunk_delay = 0.0
    backend.outage = False
    yield s
    backend.chunk_delay = 0.0
    backend.outage = False


def _finished(s, timeout: float = 120.0) -> str:
    """Wait for the council to end (answer or failure) and the composer to come back."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = s.text()
        if "Council Result" in t or "Council Failed" in t:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("council never finished\n" + s.text())
    return flows.idle(s, 30)


def _composer_rows(s) -> list[str]:
    return [line for line in s.text().splitlines() if "❯" in line]


def assert_terminal_intact(s) -> None:
    """No agent panel reached the terminal, and the composer row is the composer's."""
    assert LIVE_PANEL_TITLE not in s.raw, "an agent drew its own live panel on the terminal"
    t = s.text()
    assert "Traceback" not in t, t
    assert t.count(flows.COMPOSER) == 1, "status chrome duplicated or lost\n" + t
    for row in _composer_rows(s):
        after_prompt = row.split("❯", 1)[1]
        assert "╭" not in after_prompt and "Deliberating" not in after_prompt, row


def assert_next_prompt_processed(s) -> None:
    before = s.text().count("Done.")
    flows.ask(s, "give me MULTILINE")
    s.wait_for("Done.", 40)
    flows.idle(s)
    assert s.text().count("Done.") == before + 1
    assert_terminal_intact(s)


def test_council_run_shows_the_answer_and_never_paints_over_the_terminal(council_repl):
    s = council_repl
    flows.run_command(s, TASK)
    _finished(s)

    t = s.text()
    assert "Council Result" in t
    assert "FAKE-REPLY-OK" in t  # the final answer is on screen
    assert "▸ " in s.raw  # phase progress was appended to the transcript
    assert_terminal_intact(s)
    assert_next_prompt_processed(s)


def test_council_run_leaves_a_usable_composer(council_repl):
    s = council_repl
    flows.run_command(s, TASK)
    _finished(s)
    s.type("still-typing")
    s.wait_for("❯ still-typing", 5)
    assert_terminal_intact(s)


def test_repeated_council_runs_do_not_accumulate_output_or_requests(council_repl, backend):
    s = council_repl
    spent: list[int] = []
    for _ in range(2):
        before = len(backend.requests)
        flows.run_command(s, TASK)
        _finished(s)
        spent.append(len(backend.requests) - before)
        assert_terminal_intact(s)
    assert spent[0] == spent[1] > 0, f"second run made a different number of calls: {spent}"
    assert_next_prompt_processed(s)


@pytest.mark.parametrize(("rows", "cols"), [(24, 60), (30, 80)])
def test_council_run_on_narrow_terminals_stays_intact(council_repl, rows, cols):
    s = council_repl
    s.resize(rows, cols)
    flows.wait_layout(s)
    flows.run_command(s, TASK)
    _finished(s)
    assert_terminal_intact(s)
    assert_next_prompt_processed(s)


def test_cancelling_a_council_run_stops_the_work_and_restores_the_prompt(council_repl, backend):
    s = council_repl
    backend.chunk_delay = 3.0  # keep every seat busy long enough to interrupt
    flows.run_command(s, TASK)
    s.wait_for("Planner", 60)
    time.sleep(1.0)

    s.key("ctrl-c")
    s.wait_for("Council run interrupted.", 15)
    flows.idle(s, 20)
    settled = len(backend.requests)
    time.sleep(8.0)  # an orphaned seat would keep calling the provider
    assert len(backend.requests) == settled, "provider calls continued after cancellation"
    assert_terminal_intact(s)

    backend.chunk_delay = 0.0
    s.type("xyz")
    s.wait_for("❯ xyz", 5)
    for _ in range(3):
        s.key("backspace")
    assert_next_prompt_processed(s)
    assert s.alive()


def test_a_failing_provider_ends_the_run_cleanly_and_the_next_prompt_works(council_repl, backend):
    s = council_repl
    backend.outage = True
    flows.run_command(s, TASK)
    _finished(s, timeout=180)
    assert "Council Failed" in s.text() or "could not produce an answer" in s.text()
    assert_terminal_intact(s)

    backend.outage = False
    assert_next_prompt_processed(s)
