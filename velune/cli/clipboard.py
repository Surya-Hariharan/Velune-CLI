"""OSC 52 clipboard writes — ask the terminal itself to set the system clipboard.

OSC 52 (``ESC ] 52 ; c ; <base64> BEL``) works over SSH and inside tmux/screen
without any X11/xclip/pbcopy dependency, which is why terminal TUIs (vim,
tmux, kitty, iTerm2, Windows Terminal) use it instead of shelling out to a
platform clipboard tool.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any

# Most terminals cap the OSC 52 payload somewhere around this size; a much
# larger paste can wedge the escape-sequence parser on some emulators
# (notably older xterm) instead of failing cleanly, so truncate rather than
# risk that.
_MAX_PAYLOAD_BYTES = 100_000

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)


def build_osc52_sequence(text: str) -> str:
    """Build the raw escape sequence that sets the clipboard to *text*."""
    payload = text.encode("utf-8")[:_MAX_PAYLOAD_BYTES]
    encoded = base64.b64encode(payload).decode("ascii")
    seq = f"\x1b]52;c;{encoded}\x07"
    # Inside tmux, OSC sequences written by the child program are swallowed
    # unless wrapped in tmux's passthrough envelope — any literal ESC inside
    # must be doubled, per tmux's DCS passthrough grammar — otherwise the
    # clipboard write silently never reaches the outer terminal.
    if os.environ.get("TMUX"):
        seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    return seq


def copy_to_clipboard(app: Any, text: str) -> bool:
    """Write *text* to the system clipboard via OSC 52 through *app*'s output.

    Returns False when there is nothing to copy or no live application output
    to write through, so callers can report "nothing to copy" instead of
    raising — a failed clipboard write must never interrupt the REPL.
    """
    if not text or app is None or getattr(app, "output", None) is None:
        return False
    try:
        app.output.write_raw(build_osc52_sequence(text))
        app.output.flush()
        return True
    except Exception:
        return False


def extract_last_code_block(text: str) -> str | None:
    """Return the content of the last fenced ``` code block in *text*, if any."""
    matches = _FENCE_RE.findall(text or "")
    if not matches:
        return None
    block = matches[-1].rstrip("\n")
    return block or None
