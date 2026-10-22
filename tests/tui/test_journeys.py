"""End-to-end terminal journeys against the real ``velune`` REPL in a PTY.

Every test spawns the actual program (via ``_launcher.py``) with an isolated
config/keyring/data dir and a local fake backend, types real keystrokes, and
asserts on the emulated screen. Nothing talks to a real provider and no real
credential is ever used (see ``FAKE_KEY``).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from . import flows
from .frames import ARTIFACT_DIR, capture
from .harness import FAKE_KEY, assert_no_secret

NORMAL = "NORMAL │"  # status-bar segment; exactly one per rendered frame


def assert_clean_frame(s) -> None:
    """The live chrome appears exactly once: no stale or duplicated frames."""
    t = s.text()
    assert "Traceback" not in t, t
    assert t.count(NORMAL) == 1, t


@pytest.fixture
def repl(launch):
    s = launch()
    flows.skip_onboarding(s)
    return s


@pytest.fixture
def connected(repl, backend):
    flows.connect(repl)
    repl.wait_for("OpenAI connected.", 30)
    flows.idle(repl)
    return repl


# ── 1. launch & initial rendering ───────────────────────────────────────────


def test_first_run_wizard_then_home(launch):
    s = launch()
    s.wait_for("Velune Setup", flows.STARTUP_TIMEOUT)
    s.wait_for("How would you like to use Velune?", 10)
    s.snapshot("01_wizard_welcome")

    # Esc steps back: forward one step, then return to Welcome.
    s.key("enter")
    s.wait_for("Environment", 20)
    s.wait_for("Step 2 / 8", 10)
    s.key("esc")
    s.wait_for("Step 1 / 8", 10)

    # Skip Setup -> REPL home.
    for _ in range(3):
        s.key("down")
        time.sleep(0.1)
    s.key("enter")
    s.wait_for("Continue to REPL", 20)
    s.key("enter")
    s.wait_for(flows.COMPOSER, 30)
    s.settle()
    t = s.text()
    assert "Local-first multi-model AI developer CLI" in t
    assert "no providers configured" in t
    assert_clean_frame(s)
    s.snapshot("02_home")


@pytest.mark.parametrize(
    "rows,cols", [(24, 60), (30, 80), (50, 200)], ids=["24x60", "30x80", "50x200"]
)
def test_home_renders_at_terminal_sizes(launch, rows, cols):
    s = launch(rows=rows, cols=cols)
    flows.skip_onboarding(s)
    t = s.text()
    assert "Traceback" not in t
    assert flows.COMPOSER in t, t  # composer reachable even when tiny
    assert re.search(r"❯", t), t
    s.snapshot(f"03_home_{cols}x{rows}")


# ── 2. prompt entry & submission ────────────────────────────────────────────


def test_prompt_submit_streams_reply_and_returns_to_prompt(connected):
    s = connected
    flows.ask(s, "hello there")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    t = s.text()
    assert t.count("FAKE-REPLY-OK") == 1, t
    assert "❯ hello there" in t
    assert_clean_frame(s)
    s.snapshot("04_after_reply")


def test_repeated_prompts_do_not_duplicate_or_corrupt(connected):
    s = connected
    for i in range(3):
        flows.ask(s, f"turn {i}")
        s.wait_for(re.compile(r"FAKE-REPLY-OK"), 30)
        flows.idle(s)
        s.type("x")  # composer must still accept input between turns
        s.wait_for("❯ x", 5)
        s.key("backspace")
        assert_clean_frame(s)
    # Each of the three prompts is echoed exactly once.
    t = s.text()
    for i in range(3):
        assert t.count(f"turn {i}") <= 1, t


def test_multiline_markdown_reply_renders_structure(connected):
    s = connected
    flows.ask(s, "give me MULTILINE")
    s.wait_for("Done.", 30)
    flows.idle(s)
    t = s.text()
    assert "alpha" in t and "beta" in t and "gamma" in t
    assert "print('hello')" in t
    assert "```" not in t, "raw markdown fence leaked into the rendered reply"
    assert_clean_frame(s)
    s.snapshot("05_multiline_reply")


def test_long_reply_on_narrow_terminal_keeps_chrome(launch, backend):
    s = launch(rows=24, cols=60)
    flows.skip_onboarding(s)
    flows.connect(s)
    s.wait_for("OpenAI connected.", 30)
    flows.idle(s)
    flows.ask(s, "LONGREPLY please")
    s.wait_for("word399", 40)
    flows.idle(s)
    t = s.text()
    assert flows.COMPOSER in t and NORMAL in t
    assert "Traceback" not in t
    s.snapshot("06_long_reply_60x24")
    # Still usable afterwards.
    flows.ask(s, "again")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    assert_clean_frame(s)


def test_resize_midsession_keeps_one_clean_frame(connected):
    s = connected
    original_chrome = capture(s).rows[-6:]
    for rows, cols in [(24, 60), (40, 120), (30, 90)]:
        s.resize(rows, cols)
        flows.wait_layout(s)
        assert "Traceback" not in s.text()
        assert flows.COMPOSER in s.text()
    s.resize(40, 120)
    flows.wait_layout(s)
    assert capture(s).rows[-6:] == original_chrome, (
        "status bar/composer not restored after resizing back"
    )
    flows.ask(s, "after resize")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    assert_clean_frame(s)


# ── 3. slash commands ───────────────────────────────────────────────────────


def test_help_lists_commands_and_returns_to_prompt(repl):
    s = repl
    flows.run_command(s, "/help")
    s.wait_for("to search", 20)  # footer of /help; the top of the list scrolls off
    flows.idle(s)
    t = s.text()
    assert "/exit" in t and "/help" in t
    assert_clean_frame(s)
    s.snapshot("07_help")


def test_command_palette_opens_filters_and_dismisses(repl):
    s = repl
    s.type("/hel")
    s.wait_for("COMMAND PALETTE", 10)
    assert "/help" in s.text()
    s.key("esc")
    s.wait_gone("COMMAND PALETTE", 10)
    assert_clean_frame(s)


def test_unknown_slash_command_is_recoverable(repl):
    """Not a crash, and the REPL must stay usable afterwards."""
    s = repl
    flows.run_command(s, "/zzzbogus")
    time.sleep(1.5)
    assert "Traceback" not in s.text()
    s.key("esc")
    s.key("ctrl-u")  # clear the composer
    time.sleep(0.5)
    flows.run_command(s, "/help")
    s.wait_for("to search", 20)  # footer of /help; the top of the list scrolls off
    flows.idle(s)
    assert_clean_frame(s)


def test_unknown_slash_command_gives_feedback(repl):
    """Enter on a command that matches nothing reports it and leaves a usable composer."""
    s = repl
    flows.run_command(s, "/zzzbogus")
    s.wait_for("Unknown command: /zzzbogus", 10)
    flows.idle(s)
    t = s.text()
    assert "/help" in t[t.index("Unknown command") :], "feedback should point at /help"
    assert "No matching commands" not in t, "dead palette left open"
    assert "COMMAND PALETTE" not in t
    assert s.alive()
    # Composer is empty with the cursor on it, and typing works straight away.
    f = capture(s)
    assert f.rows[f.cursor.y].startswith("│ ❯") and not f.cursor.hidden
    s.type("still works")
    s.wait_for("❯ still works", 5)
    assert_clean_frame(s)


def test_unknown_command_with_arguments_gets_the_same_feedback(repl):
    flows.run_command(repl, "/nosuchthing with some args")
    repl.wait_for("Unknown command: /nosuchthing", 10)
    flows.idle(repl)
    assert_clean_frame(repl)


def test_known_command_still_runs_from_the_palette(repl):
    """The fix must not change the matching path: a partial name still selects and runs."""
    repl.type("/hel")
    repl.wait_for("COMMAND PALETTE", 10)
    time.sleep(0.4)
    repl.key("enter")
    repl.wait_for("to search", 20)
    assert "Unknown command" not in repl.text()


# ── 4. API-key configuration ────────────────────────────────────────────────


def test_connect_valid_fake_key_masks_input_and_never_leaks(repl, backend, tmp_path):
    s = repl
    flows.run_command(s, "/connect")
    s.wait_for("CONNECT PROVIDER", 20)
    s.snapshot("08_connect_picker")
    s.type("openai")
    time.sleep(0.6)
    s.key("enter")
    s.wait_for("OpenAI API key", 20)
    s.type(FAKE_KEY, delay=0.003)
    time.sleep(0.4)
    typed = s.text()
    assert FAKE_KEY not in typed, "key echoed in clear while typing"
    assert "*" in typed, "key entry is not visibly masked"
    s.snapshot("09_connect_key_masked")
    s.key("enter")
    s.wait_for("OpenAI connected.", 30)
    flows.idle(s)
    assert_no_secret(s)
    assert_clean_frame(s)

    # Persisted state must not hold the plaintext either.
    cred = tmp_path / "sandbox" / "config" / "credentials.json"
    assert cred.exists(), "credential store was not written to the isolated config dir"
    assert FAKE_KEY not in cred.read_text(encoding="utf-8", errors="replace")

    # Nothing left the machine: every request was rerouted from a known host.
    origins = {h for _, _, h in backend.requests if h}
    assert origins == {"api.openai.com"}, origins  # non-empty: the reroute really carried traffic
    for art in ARTIFACT_DIR.glob("*") if ARTIFACT_DIR.exists() else []:
        assert FAKE_KEY not in art.read_text(encoding="utf-8", errors="replace"), art


def test_connect_malformed_key_is_rejected_client_side(repl, backend):
    """Groq keys start with 'gsk_'; an 'sk-' key must be refused with no request."""
    before = len(backend.requests)
    s = repl
    flows.connect(s, provider="groq", key=FAKE_KEY)
    s.wait_for(re.compile(r"gsk_"), 20)
    s.wait_for("NOT CONNECTED", 10)
    assert len(backend.requests) == before, "a malformed key still triggered a network call"
    assert_no_secret(s)
    s.snapshot("10_connect_malformed")
    # Cancel returns to the prompt.
    s.type("Cancel")
    time.sleep(0.5)
    s.key("enter")
    flows.idle(s)
    assert_clean_frame(s)


def test_connect_rejected_key_then_back_to_prompt(repl):
    bad = "sk-" + "x" * 30
    s = repl
    flows.connect(s, provider="openai", key=bad)
    s.wait_for("NOT CONNECTED", 30)
    assert bad not in s.text()
    s.snapshot("11_connect_rejected")
    s.key("esc")
    flows.idle(s)
    assert "CONNECT PROVIDER" not in s.text()
    assert_clean_frame(s)


# ── 5. navigation & repeated workflows ──────────────────────────────────────


def test_esc_backs_out_of_connect_and_repeats_cleanly(repl):
    s = repl
    for _ in range(5):
        flows.run_command(s, "/connect")
        s.wait_for("CONNECT PROVIDER", 20)
        s.key("esc")
        s.wait_gone("CONNECT PROVIDER", 10)
        s.settle()
        s.key("ctrl-u")
        time.sleep(0.2)
    flows.idle(s)
    assert_clean_frame(s)
    assert s.alive()


def test_back_from_key_prompt_returns_to_picker_then_prompt(repl):
    s = repl
    flows.run_command(s, "/connect")
    s.wait_for("CONNECT PROVIDER", 20)
    s.type("openai")
    time.sleep(0.6)
    s.key("enter")
    s.wait_for("OpenAI API key", 20)
    s.key("esc")  # back out of the key prompt
    time.sleep(1.0)
    s.key("esc")  # and out of the flow
    flows.idle(s)
    assert "API key ❯" not in s.text()
    assert_clean_frame(s)


def test_new_session_command_resets_view(connected):
    s = connected
    flows.ask(s, "first")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    flows.run_command(s, "/new")
    flows.idle(s)
    assert_clean_frame(s)
    flows.ask(s, "second")
    s.wait_for("FAKE-REPLY-OK", 30)
    flows.idle(s)
    assert_clean_frame(s)


# ── 6. exit & terminal restoration ──────────────────────────────────────────


_MODES = (1049, 1000, 1006, 2004)  # alt-screen, mouse click, SGR mouse, bracketed paste


def _mode_balance(raw: str) -> dict[int, tuple[int, int]]:
    return {n: (raw.count(f"[?{n}h"), raw.count(f"[?{n}l")) for n in _MODES}


def assert_terminal_restored(raw: str) -> None:
    """Platform-neutral checks on the raw byte stream the program wrote.

    * every mode that was switched on was switched off, and the *last* event for
      each mode is the disable (so nothing is re-enabled during shutdown);
    * the cursor is visible again, with that being its final state.

    This is what the program asked the terminal to do. It cannot see the host
    console's own mode flags (Windows) or termios state (POSIX); see README.
    """
    for mode, (on, off) in _mode_balance(raw).items():
        assert on == off and on >= 1, f"mode ?{mode}: enabled {on}x, disabled {off}x"
        assert raw.rfind(f"[?{mode}l") > raw.rfind(f"[?{mode}h"), f"mode ?{mode} re-enabled at exit"
    assert raw.rfind("[?25h") > raw.rfind("[?25l"), "cursor left hidden at exit"


def test_slash_exit_exits_zero_and_restores_terminal(repl):
    s = repl
    flows.run_command(s, "/exit")
    assert s.wait_exit(20), "process did not exit after /exit"
    assert s._p.exitstatus == 0
    assert_terminal_restored(s.raw)


def test_double_ctrl_c_exits_and_restores_terminal(repl):
    s = repl
    s.key("ctrl-c")
    time.sleep(0.3)
    s.key("ctrl-c")
    assert s.wait_exit(20), "double Ctrl+C did not exit"
    assert_terminal_restored(s.raw)


def test_exit_after_a_popup_cancel_still_restores_terminal(repl):
    """Leaving mid-flow state behind (cancelled popup) must not leak terminal modes."""
    s = repl
    flows.run_command(s, "/connect")
    s.wait_for("CONNECT PROVIDER", 20)
    s.key("esc")
    s.wait_gone("CONNECT PROVIDER", 10)
    flows.run_command(s, "/exit")
    assert s.wait_exit(20)
    assert_terminal_restored(s.raw)


def test_ctrl_c_on_text_clears_composer_without_exiting(repl):
    s = repl
    s.type("some half typed prompt")
    s.key("ctrl-c")
    time.sleep(1.0)
    assert s.alive()
    assert "some half typed prompt" not in s.text()
    assert_clean_frame(s)


def test_ctrl_d_is_ignored_and_never_corrupts_the_session(repl):
    """Pinned current behaviour: Ctrl+D does NOT exit (empty or with text) and loses nothing.

    If Ctrl+D-to-exit is ever added, change this test deliberately.
    """
    s = repl
    s.key("ctrl-d")
    time.sleep(1.5)
    assert s.alive(), "Ctrl+D on an empty composer exited"
    assert_clean_frame(s)
    s.type("abc")
    s.wait_for("❯ abc", 5)
    s.key("ctrl-d")
    time.sleep(1.0)
    assert s.alive()
    assert "❯ abc" in s.text(), "Ctrl+D altered the composer text"
    assert_clean_frame(s)


def test_ctrl_d_then_slash_exit_exits_cleanly(repl):
    s = repl
    s.key("ctrl-d")
    time.sleep(0.8)
    flows.run_command(s, "/exit")
    assert s.wait_exit(20)
    assert s._p.exitstatus == 0
    assert_terminal_restored(s.raw)


def test_baselines_and_artifacts_never_contain_the_key(repl, tmp_path):
    """API-key masking, end to end: screen, raw stream, snapshots, committed fixtures."""
    s = repl
    flows.connect(s)
    s.wait_for("OpenAI connected.", 30)
    flows.idle(s)
    s.snapshot("masking_check")  # writes .txt and .svg to the transient dir
    assert_no_secret(s)
    for art in (ARTIFACT_DIR / "masking_check.txt", ARTIFACT_DIR / "masking_check.svg"):
        assert FAKE_KEY not in art.read_text(encoding="utf-8", errors="replace"), art
    # Same for every committed baseline.
    from .frames import BASELINE_DIR

    for b in BASELINE_DIR.glob("*.json"):
        assert FAKE_KEY not in b.read_text(encoding="utf-8"), b
    assert not str(ARTIFACT_DIR).startswith(str(Path(__file__).parent)), (
        "artifacts must live outside the repo"
    )
