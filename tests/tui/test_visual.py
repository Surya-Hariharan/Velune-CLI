"""Approved-baseline visual regression for the real REPL, plus popup geometry.

Baselines live in ``tests/tui/baselines`` (see README for how to regenerate and
review them). Frames are compared cell-by-cell with dynamic values masked, so a
changed glyph, row, colour, bold flag or cursor position fails the test and the
failure message names the row, columns and attribute.
"""

from __future__ import annotations

import time

import pytest

from . import flows
from .frames import Frame, assert_frame, capture
from .harness import assert_no_secret

TEXT_ROWS = 40


@pytest.fixture
def repl(launch):
    s = launch()
    flows.skip_onboarding(s)
    return s


@pytest.fixture
def connected(repl):
    flows.connect(repl)
    repl.wait_for("OpenAI connected.", 30)
    flows.idle(repl)
    return repl


def _settled(s) -> None:
    s.settle(0.8)


# ── whole-screen baselines ───────────────────────────────────────────────────


def test_baseline_wizard_welcome(launch):
    s = launch()
    s.wait_for("How would you like to use Velune?", flows.STARTUP_TIMEOUT)
    _settled(s)
    assert_frame(s, "wizard_welcome")


def test_baseline_home(repl):
    _settled(repl)
    assert_frame(repl, "home_120x40")


@pytest.mark.parametrize("rows,cols", [(24, 60), (30, 80)], ids=["60x24", "80x30"])
def test_baseline_home_small(launch, rows, cols):
    s = launch(rows=rows, cols=cols)
    flows.skip_onboarding(s)
    _settled(s)
    assert_frame(s, f"home_{cols}x{rows}")


def test_baseline_after_reply(connected):
    flows.ask(connected, "hello there")
    connected.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(connected)
    assert_frame(connected, "after_reply")


def test_baseline_multiline_reply(connected):
    flows.ask(connected, "give me MULTILINE")
    connected.wait_for("Done.", 30)
    flows.idle(connected)
    assert_frame(connected, "multiline_reply")


def test_baseline_unknown_command_feedback(repl):
    flows.run_command(repl, "/zzzbogus")
    repl.wait_for("Unknown command", 10)
    flows.idle(repl)
    assert_frame(repl, "unknown_command")


def test_baseline_picker_over_empty_transcript(repl):
    flows.run_command(repl, "/connect")
    repl.wait_for("CONNECT PROVIDER", 20)
    _settled(repl)
    assert_frame(repl, "picker_over_home")


def test_baseline_picker_and_key_prompt_over_help(repl):
    flows.run_command(repl, "/help")
    repl.wait_for("to search", 20)
    flows.idle(repl)
    flows.run_command(repl, "/connect")
    repl.wait_for("CONNECT PROVIDER", 20)
    _settled(repl)
    assert_frame(repl, "picker_over_help")
    repl.type("openai")
    time.sleep(0.6)
    repl.key("enter")
    repl.wait_for("OpenAI API key", 20)
    repl.type("sk-" + "x" * 32, delay=0.003)
    _settled(repl)
    frame = assert_frame(repl, "key_prompt_over_help")
    assert "sk-xxxx" not in frame.text()
    assert "*" * 20 in frame.text(), "typed key is not masked in the rendered frame"


# ── popup geometry (Task 2) ──────────────────────────────────────────────────


def popup_rect(f: Frame) -> tuple[int, int, int, int]:
    """(top, bottom, left, right) of the popup border, found from its corner glyphs."""
    top = next(y for y, r in enumerate(f.rows) if "┌" in r and "┐" in r)
    bottom = next(y for y, r in enumerate(f.rows) if "└" in r and "┘" in r)
    return top, bottom, f.rows[top].index("┌"), f.rows[top].rindex("┐")


def _cells(row: str, a: int, b: int) -> str:
    return row.ljust(b)[a:b]


def assert_popup_is_clean(popup: Frame, closed: Frame) -> tuple[int, int, int, int]:
    """The popup is a closed box, and everything outside it is *unchanged transcript*.

    ``closed`` is the frame after the popup was dismissed. The transcript has the
    same scroll position then (the ``❯ /connect`` echo is appended before the popup
    opens), so any difference outside the popup rectangle would be residue.
    """
    top, bottom, left, right = popup_rect(popup)
    assert (top, bottom) == (1, 18), f"popup moved: rows {top}-{bottom}"
    assert 0 <= left and right < popup.cols
    for y in range(top + 1, bottom):
        row = popup.rows[y]
        assert row[left] == "│" and row[right] == "│", f"row {y}: broken side border {row!r}"
    for y in (top, bottom):
        assert popup.rows[y].count("┌") + popup.rows[y].count("└") == 1, (
            f"duplicated corner, row {y}"
        )
    # Rows above and below the popup, and the gutters beside it, equal the frame
    # taken before it opened: the "fragments" are the real transcript, not residue.
    for y in range(0, top):
        assert popup.rows[y] == closed.rows[y], f"row {y} above popup changed"
    for y in range(top, bottom + 1):
        assert _cells(popup.rows[y], 0, left) == _cells(closed.rows[y], 0, left), (
            f"left gutter row {y}"
        )
        assert _cells(popup.rows[y], right + 1, popup.cols) == _cells(
            closed.rows[y], right + 1, popup.cols
        ), f"right gutter row {y}"
    for y in range(bottom + 1, popup.n_rows - 6):
        assert popup.rows[y] == closed.rows[y], f"row {y} below popup changed"
    return top, bottom, left, right


def _no_popup_left(s) -> None:
    t = s.text()
    for glyph in ("┌", "┐", "└", "┘", "CONNECT PROVIDER", "API key ❯"):
        assert glyph not in t, f"popup residue {glyph!r} on screen:\n{t}"


def _composer_empty(s) -> None:
    f = capture(s)
    assert any(r.rstrip() == "│ ❯" for r in f.rows), "composer is not empty"
    assert not f.cursor.hidden
    assert f.rows[f.cursor.y].startswith("│ ❯"), f"cursor left the composer: row {f.cursor.y}"


def test_popup_over_help_transcript_is_clean_and_fragments_are_real_transcript(repl):
    flows.run_command(repl, "/help")
    repl.wait_for("to search", 20)
    flows.idle(repl)

    repl.type("/connect")
    time.sleep(0.6)
    repl.key("enter")
    repl.wait_for("CONNECT PROVIDER", 20)
    _settled(repl)
    picker = capture(repl)

    repl.type("openai")
    time.sleep(0.6)
    repl.key("enter")
    repl.wait_for("OpenAI API key", 20)
    repl.type("sk-" + "k" * 30, delay=0.003)
    _settled(repl)
    key_prompt = capture(repl)

    repl.key("esc")
    repl.wait_gone("OpenAI API key", 10)
    _settled(repl)
    closed = capture(repl)
    # The transcript left visible around each popup is exactly what remains once
    # it closes: the gutter "fragments" are real transcript, not stale cells.
    assert_popup_is_clean(picker, closed)
    assert_popup_is_clean(key_prompt, closed)


def test_escape_at_every_step_restores_an_empty_composer(repl):
    # Step 1: picker -> Esc
    flows.run_command(repl, "/connect")
    repl.wait_for("CONNECT PROVIDER", 20)
    repl.key("esc")
    repl.wait_gone("CONNECT PROVIDER", 10)
    _settled(repl)
    _no_popup_left(repl)
    _composer_empty(repl)

    # Step 2: picker -> filter -> select -> key prompt -> Esc
    flows.run_command(repl, "/connect")
    repl.wait_for("CONNECT PROVIDER", 20)
    repl.type("gro")
    repl.wait_for("1 result", 10)
    assert "Groq" in repl.text()
    repl.key("enter")
    repl.wait_for("Groq API key", 20)
    repl.type("gsk_" + "p" * 20, delay=0.003)
    repl.key("esc")
    repl.wait_gone("Groq API key", 10)
    _settled(repl)
    _no_popup_left(repl)
    _composer_empty(repl)
    assert "gsk_pppp" not in repl.raw, "half-typed key leaked after Esc"


def test_reopening_the_picker_repeatedly_does_not_accumulate_artifacts(repl):
    flows.run_command(repl, "/help")
    repl.wait_for("to search", 20)
    flows.idle(repl)

    chrome: list[list[str]] = []
    inside: list[list[str]] = []
    cycles = 5
    for i in range(cycles):
        repl.type("/connect")
        time.sleep(0.5)
        repl.key("enter")
        repl.wait_for("CONNECT PROVIDER", 20)
        _settled(repl)
        popup = capture(repl)
        repl.key("esc")
        repl.wait_gone("CONNECT PROVIDER", 10)
        _settled(repl)
        top, bottom, left, right = assert_popup_is_clean(popup, capture(repl))
        inside.append([r[left : right + 1] for r in popup.rows[top : bottom + 1]])
        _no_popup_left(repl)
        _composer_empty(repl)
        chrome.append(capture(repl).rows[-6:])
        assert repl.text().count("❯ /connect") == i + 1, "cancelled command echo count drifted"

    assert all(c == chrome[0] for c in chrome), "status bar/composer changed between cycles"
    assert all(p == inside[0] for p in inside), "popup contents changed between identical openings"


def test_input_works_immediately_after_cancelling_a_popup(connected):
    s = connected
    flows.run_command(s, "/connect")
    s.wait_for("CONNECT PROVIDER", 20)
    s.key("esc")
    s.wait_gone("CONNECT PROVIDER", 10)
    s.type("typed right after cancel")
    s.wait_for("❯ typed right after cancel", 5)
    s.key("enter")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    _composer_empty(s)
    assert_no_secret(s)
