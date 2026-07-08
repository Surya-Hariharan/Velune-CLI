"""Interactive-terminal capability check.

``prompt_toolkit.Application.run_async()`` hangs or misbehaves when stdin is
piped (no real terminal to read key events from), so every entry point that
can be invoked non-interactively (``velune onboard``, ``velune setup``, and
any CI/script/pipe usage of either) must check this first and fall back to a
plain linear prompt sequence instead of constructing a ``WizardController``.
"""

from __future__ import annotations

import sys


def is_interactive_tty() -> bool:
    """True when both stdin and stdout are real terminals."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
