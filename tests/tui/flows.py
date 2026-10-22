"""Reusable user journeys on top of :class:`TermSession`."""

from __future__ import annotations

import time

from .harness import FAKE_KEY, TermSession

COMPOSER = "NORMAL │"  # status bar is present at every width (the footer hint is not)
STARTUP_TIMEOUT = 60.0  # cold imports on a slow CI box


def skip_onboarding(s: TermSession) -> None:
    """First-run wizard -> "Skip Setup" -> "Continue to REPL" -> idle prompt."""
    s.wait_for("Skip Setup", STARTUP_TIMEOUT)
    for _ in range(3):
        s.key("down")
        time.sleep(0.1)
    s.key("enter")
    s.wait_for("Continue to REPL", 20)
    s.key("enter")
    s.wait_for(COMPOSER, 30)
    s.settle()


def run_command(s: TermSession, cmd: str) -> None:
    """Type a slash command and submit it (the palette opens as you type)."""
    s.type(cmd)
    time.sleep(0.6)
    s.key("enter")


def connect(s: TermSession, provider: str = "openai", key: str = FAKE_KEY) -> None:
    """Drive ``/connect`` through to the key prompt and submit *key*."""
    run_command(s, "/connect")
    s.wait_for("CONNECT PROVIDER", 20)
    s.type(provider)
    time.sleep(0.6)
    s.key("enter")
    s.wait_for("API key", 20)
    s.type(key, delay=0.003)
    time.sleep(0.3)
    s.key("enter")


def ask(s: TermSession, prompt: str) -> None:
    s.type(prompt)
    s.key("enter")


def idle(s: TermSession, timeout: float = 30.0) -> str:
    """Wait until the idle composer is back and the screen has stopped moving."""
    s.wait_for(COMPOSER, timeout)
    return s.settle()


def wait_layout(s: TermSession, timeout: float = 10.0) -> None:
    """After a resize, wait until the REPL has re-laid out for the new size.

    The status bar sits 6 rows from the bottom and the cursor on the composer's
    first row. The emulator keeps stale cells on resize, so "the composer text is
    on screen" proves nothing, and the redraw lands ~1s after the resize (measured
    0.64-1.04s on Windows), so a fixed settle can return on a blank, unchanged frame.
    """
    rows = s.rows
    end = time.time() + timeout
    while time.time() < end:
        with s._lock:
            status = "".join(s.screen.buffer[rows - 6][x].data for x in range(s.screen.columns))
            cursor_row = s.screen.cursor.y
        if "NORMAL" in status and cursor_row == rows - 4:
            s.settle(0.3)
            return
        time.sleep(0.02)
    raise AssertionError(
        f"layout not restored {timeout}s after resize to {rows} rows" + "\n" + s.text()
    )
