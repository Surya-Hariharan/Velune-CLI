"""Detects the host terminal emulator and reports whether it can be asked,
for the duration of a Velune session, to stop handling its own font-zoom
shortcuts (Ctrl +/-/0, Ctrl+scroll, Cmd +/-/0, etc.).

Short answer, for every emulator below: no. Zoom is decided by the emulator's
own window/toolkit event loop — Windows Terminal's XAML input pipeline, GTK's
accelerator table in GNOME Terminal, Qt's shortcut system in Konsole, winit's
event loop in Alacritty, Cocoa's menu key-equivalents in Terminal.app/iTerm2,
VS Code's keybinding service — and it's consumed there *before* any byte
reaches the PTY that feeds this process. Velune (or any other program
attached to that PTY) never sees the keystroke or wheel event, so there is no
input to intercept, no escape sequence to answer with, and no session-scoped
API to negotiate it away. The only lever that exists anywhere in this list is
the emulator's own persistent, global keybinding config — off-limits here
both because Velune must not touch it and because a config edit doesn't
satisfy "session-scoped, reverts automatically on exit" anyway.

See ``docs/terminal-zoom-lock.md`` for the full per-terminal writeup this
module's ``reason`` strings are drawn from.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalInfo:
    """What terminal Velune thinks it's running inside, and whether this
    process could plausibly suppress that terminal's own zoom shortcuts."""

    name: str
    zoom_lock_supported: bool
    reason: str


# Always False today — kept as a field (not a bare constant) so a future
# terminal that genuinely ships a session-scoped "suppress your own zoom
# accelerators" API has somewhere to report `True` without changing the
# shape callers depend on.
_NOT_SUPPORTED = False

_NO_SESSION_API = (
    "handled entirely inside the terminal emulator's own input pipeline, "
    "before any byte reaches this process over the PTY — there is no "
    "session-scoped API for a child process to intercept or suppress it."
)


def detect_terminal(
    env: Mapping[str, str] | None = None, platform: str | None = None
) -> TerminalInfo:
    """Best-effort identification of the terminal emulator hosting this
    process, purely from environment variables (the only signal available
    without querying the emulator directly, which none of them expose an API
    for). ``env``/``platform`` are injectable for testing; default to the
    real process environment and ``sys.platform``.
    """
    e = env if env is not None else os.environ
    plat = platform if platform is not None else sys.platform

    term_program = e.get("TERM_PROGRAM", "")

    if term_program == "vscode":
        return TerminalInfo(
            "VS Code Integrated Terminal",
            _NOT_SUPPORTED,
            "Ctrl/Cmd +/-/0 are VS Code's own 'Terminal: Increase/Decrease/Reset "
            "Font Size' commands, resolved by VS Code's keybinding service before "
            "its xterm.js terminal (and long before the PTY) ever sees the "
            "keystroke — " + _NO_SESSION_API + " A VS Code extension could "
            "contribute a keybinding override, but that's a separate, "
            "persistent, opt-in IDE artifact — not something this CLI process "
            "can install or revert for itself.",
        )
    if term_program == "iTerm.app":
        return TerminalInfo(
            "iTerm2",
            _NOT_SUPPORTED,
            "Cmd +/-/0 and Ctrl+scroll are iTerm2's own menu key-equivalents and "
            "font-size actions — " + _NO_SESSION_API + " iTerm2's scripting API "
            "controls tabs/panes/text, not the app's own keyboard shortcut "
            "table; only iTerm2's persistent Preferences > Keys can rebind "
            "them, which Velune will not touch.",
        )
    if term_program == "Apple_Terminal":
        return TerminalInfo(
            "macOS Terminal.app",
            _NOT_SUPPORTED,
            "Cmd +/-/0 are Terminal.app's own Cocoa menu key-equivalents — "
            + _NO_SESSION_API
            + " Only the user's persistent Keyboard Shortcuts or Terminal "
            "preferences can change them.",
        )
    if "WT_SESSION" in e:
        return TerminalInfo(
            "Windows Terminal",
            _NOT_SUPPORTED,
            "Ctrl +/-/0 and Ctrl+scroll are Windows Terminal's built-in "
            "increaseFontSize/decreaseFontSize/resetFontSize actions, resolved "
            "by its own input handling before ConPTY forwards anything to this "
            "process — " + _NO_SESSION_API + " Only the user's persistent "
            "settings.json 'actions' list can rebind or remove them.",
        )
    if "KONSOLE_VERSION" in e:
        return TerminalInfo(
            "Konsole",
            _NOT_SUPPORTED,
            "Ctrl +/-/0 (and Ctrl+scroll) are Konsole's own Qt keyboard "
            "shortcuts, consumed by its session controller before VT100 "
            "processing — " + _NO_SESSION_API + " Only Konsole's persistent "
            "'Configure Shortcuts' can change them.",
        )
    if e.get("GNOME_TERMINAL_SCREEN") or e.get("GNOME_TERMINAL_SERVICE"):
        return TerminalInfo(
            "GNOME Terminal",
            _NOT_SUPPORTED,
            "Ctrl +/-/0 are GTK accelerator actions ('zoom-in'/'zoom-out'/"
            "'zoom-normal') wired at the GtkApplication level, consumed before "
            "the VTE terminal widget ever sees the keypress — "
            + _NO_SESSION_API
            + " Only a persistent gsettings/dconf keybinding override can "
            "change them.",
        )
    if "KITTY_WINDOW_ID" in e or e.get("TERM") == "xterm-kitty":
        return TerminalInfo(
            "kitty",
            _NOT_SUPPORTED,
            "Font-size shortcuts are kitty's own key mappings, resolved in its "
            "event loop before any PTY write. kitty's remote-control protocol "
            "(kitty @) can change the *current* font size if the user has "
            "already opted into allow_remote_control in their own kitty.conf, "
            "but that still can't suppress the shortcut itself — only "
            "re-snap the size after each press, which is an unreliable race, "
            "not a lock. Velune declines to implement that guess-and-correct "
            "workaround.",
        )
    if "ALACRITTY_SOCKET" in e or e.get("TERM") == "alacritty":
        return TerminalInfo(
            "Alacritty",
            _NOT_SUPPORTED,
            "IncreaseFontSize/DecreaseFontSize/ResetFontSize are key bindings "
            "resolved entirely inside Alacritty's own winit event loop — "
            + _NO_SESSION_API
            + " Only editing the user's persistent alacritty.toml can change "
            "them.",
        )

    if plat == "win32":
        # Neither Windows Terminal nor a recognizable TERM_PROGRAM: this is
        # the legacy Console Host (conhost.exe), whether it's fronting
        # PowerShell or cmd.exe — `PSModulePath` is set by PowerShell in
        # every edition (Desktop and Core) and absent under plain cmd.exe.
        shell = "PowerShell" if "PSModulePath" in e else "Command Prompt"
        return TerminalInfo(
            f"Windows Console Host ({shell})",
            _NOT_SUPPORTED,
            "Ctrl+scroll font resizing is a conhost.exe feature handled inside "
            "the console host before input is delivered to this process — "
            + _NO_SESSION_API
            + " `SetConsoleMode`/`GetConsoleMode` control how input is "
            "*reported* to an app (line/echo/mouse modes), not whether the "
            "host's own zoom gesture fires; only the console window's "
            "persistent Properties dialog or HKCU\\Console registry keys "
            "control it, and Velune will not touch either.",
        )

    return TerminalInfo(
        e.get("TERM") or "unknown terminal",
        _NOT_SUPPORTED,
        "Terminal emulator not recognized from environment variables. As "
        "with every terminal Velune does recognize, any font-zoom shortcut "
        "this one implements would be handled in the emulator's own input "
        "layer, above the PTY — " + _NO_SESSION_API,
    )
