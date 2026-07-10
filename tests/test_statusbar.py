"""Tests for velune.cli.statusbar — provider, git branch, and MCP segments."""

from __future__ import annotations

from velune.cli.statusbar import StatusBarState, render_status_bar


def _text(state: StatusBarState) -> str:
    return "".join(t for _s, t in render_status_bar(state))


def test_provider_prefixes_model():
    state = StatusBarState(model_id="llama-3.3-70b", provider_id="groq")
    text = _text(state)
    assert "groq·llama-3.3-70b" in text.replace(" ", "")


def test_model_without_provider_renders_plain():
    text = _text(StatusBarState(model_id="llama-3.3-70b"))
    assert "llama-3.3-70b" in text
    assert "·llama" not in text.replace(" ", "")


def test_git_branch_shown_only_inside_a_repo():
    assert "main" in _text(StatusBarState(git_branch="main"))
    assert "non-git" not in _text(StatusBarState(git_branch="non-git"))
    assert "unknown" not in _text(StatusBarState(git_branch="unknown"))


def test_mcp_silent_when_no_servers_configured():
    assert "mcp" not in _text(StatusBarState())


def test_mcp_counts_shown_when_configured():
    text = _text(StatusBarState(mcp_connected=2, mcp_total=3))
    assert "mcp 2/3" in text


def test_mcp_degraded_uses_warn_style():
    fragments = render_status_bar(StatusBarState(mcp_connected=1, mcp_total=2))
    mcp_frag = next((s, t) for s, t in fragments if "mcp" in t)
    assert "warn" in mcp_frag[0]

    fragments = render_status_bar(StatusBarState(mcp_connected=2, mcp_total=2))
    mcp_frag = next((s, t) for s, t in fragments if "mcp" in t)
    assert "ok" in mcp_frag[0]
