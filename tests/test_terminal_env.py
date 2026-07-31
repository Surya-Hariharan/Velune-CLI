"""Terminal-emulator detection for the zoom-lock feasibility report.

Every branch must report `zoom_lock_supported=False` — that's the whole
point of the investigation this module encodes (see
docs/terminal-zoom-lock.md): no terminal in the supported list exposes a
session-scoped API for a child process to suppress its own font-zoom
shortcuts. These tests exist to (a) lock in the env-var detection rules and
(b) guard against a future edit accidentally flipping one branch to `True`
without an actual, verified capability behind it.
"""

from __future__ import annotations

from velune.cli.terminal_env import detect_terminal


def test_windows_terminal_detected_via_wt_session():
    info = detect_terminal(env={"WT_SESSION": "abc"}, platform="win32")
    assert info.name == "Windows Terminal"
    assert info.zoom_lock_supported is False
    assert info.reason


def test_vscode_integrated_terminal_detected():
    info = detect_terminal(env={"TERM_PROGRAM": "vscode"}, platform="linux")
    assert info.name == "VS Code Integrated Terminal"
    assert info.zoom_lock_supported is False


def test_iterm2_detected():
    info = detect_terminal(env={"TERM_PROGRAM": "iTerm.app"}, platform="darwin")
    assert info.name == "iTerm2"
    assert info.zoom_lock_supported is False


def test_macos_terminal_app_detected():
    info = detect_terminal(env={"TERM_PROGRAM": "Apple_Terminal"}, platform="darwin")
    assert info.name == "macOS Terminal.app"
    assert info.zoom_lock_supported is False


def test_konsole_detected():
    info = detect_terminal(env={"KONSOLE_VERSION": "24.0"}, platform="linux")
    assert info.name == "Konsole"
    assert info.zoom_lock_supported is False


def test_gnome_terminal_detected():
    info = detect_terminal(env={"GNOME_TERMINAL_SCREEN": "/x"}, platform="linux")
    assert info.name == "GNOME Terminal"
    assert info.zoom_lock_supported is False


def test_kitty_detected_via_window_id():
    info = detect_terminal(env={"KITTY_WINDOW_ID": "1"}, platform="linux")
    assert info.name == "kitty"
    assert info.zoom_lock_supported is False


def test_kitty_detected_via_term():
    info = detect_terminal(env={"TERM": "xterm-kitty"}, platform="linux")
    assert info.name == "kitty"


def test_alacritty_detected_via_socket():
    info = detect_terminal(env={"ALACRITTY_SOCKET": "/tmp/x"}, platform="linux")
    assert info.name == "Alacritty"
    assert info.zoom_lock_supported is False


def test_wezterm_detected_via_executable_env_var():
    info = detect_terminal(env={"WEZTERM_EXECUTABLE": "/usr/bin/wezterm"}, platform="linux")
    assert info.name == "WezTerm"
    assert info.zoom_lock_supported is False
    assert info.reason


def test_wezterm_detected_via_pane_env_var():
    info = detect_terminal(env={"WEZTERM_PANE": "0"}, platform="darwin")
    assert info.name == "WezTerm"
    assert info.zoom_lock_supported is False


def test_windows_console_host_powershell():
    info = detect_terminal(env={"PSModulePath": "C:\\x"}, platform="win32")
    assert info.name == "Windows Console Host (PowerShell)"
    assert info.zoom_lock_supported is False


def test_windows_console_host_cmd():
    info = detect_terminal(env={}, platform="win32")
    assert info.name == "Windows Console Host (Command Prompt)"
    assert info.zoom_lock_supported is False


def test_unrecognized_terminal_falls_back_gracefully():
    info = detect_terminal(env={"TERM": "screen-256color"}, platform="linux")
    assert info.name == "screen-256color"
    assert info.zoom_lock_supported is False
    assert info.reason


def test_wholly_unknown_terminal_still_reports_something():
    info = detect_terminal(env={}, platform="linux")
    assert info.name == "unknown terminal"
    assert info.zoom_lock_supported is False
