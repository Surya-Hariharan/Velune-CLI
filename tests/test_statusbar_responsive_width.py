"""`render_status_bar` must degrade by dropping lower-priority segments as
the terminal narrows, never by hard-clipping mid-segment.

Regression coverage for the "looks polished at 90% zoom, not at 100%" report:
zoom itself is not observable by a terminal app (see docs/terminal-zoom-lock.md)
— a narrower zoom level is really just fewer effective columns, and the
status bar previously concatenated every active segment with no width
awareness at all, letting prompt_toolkit's non-wrapping height=1 status
window hard-clip the tail whenever too many indicators were active for the
available width.
"""

from __future__ import annotations

from velune.cli.statusbar import StatusBarState, render_status_bar


def _text(fragments) -> str:
    return "".join(t for _s, t in fragments)


def _busy_state() -> StatusBarState:
    """Every optional segment active at once — the worst case for width."""
    return StatusBarState(
        model_id="claude-opus-4-6-20260514",
        provider_id="anthropic",
        mode_label="MAX",
        context_pct=42.0,
        context_used=42_000,
        context_max=100_000,
        git_branch="feature/responsive-layout",
        mcp_connected=2,
        mcp_total=3,
        bg_job_count=2,
        alert_count=1,
        invalid_keys=("openai",),
        provider_health="degraded",
        last_latency_ms=1234.0,
        last_tokens_per_sec=42.0,
    )


def test_unbounded_width_shows_every_segment():
    text = _text(render_status_bar(_busy_state()))
    for expected in (
        "anthropic",
        "claude-opus-4-6-20260514",
        "MAX",
        "ctx 42%",
        "feature/responsive-layout",
        "mcp 2/3",
        "bg:2",
        "alerts:1",
        "openai key invalid",
        "provider degraded",
        "1.2s",
        "42t/s",
    ):
        assert expected in text, f"{expected!r} missing at unbounded width"


def test_rendered_line_never_exceeds_the_given_width():
    state = _busy_state()
    for width in (40, 60, 80, 90, 100, 110, 120, 150, 200):
        text = _text(render_status_bar(state, width))
        assert len(text) <= width, f"width={width}: line is {len(text)} chars ({text!r})"


def test_narrow_width_drops_low_priority_segments_instead_of_clipping():
    state = _busy_state()
    text = _text(render_status_bar(state, 55))
    # Core identity must survive even under real width pressure.
    assert "MAX" in text
    assert "ctx 42%" in text
    # No fragment may end mid-word — every group is added atomically or not
    # at all, so the line must not end on a truncated token like "alert" or
    # "prov".
    assert not text.rstrip().endswith(("alert", "prov", "invali", "degrad"))


def test_dropped_segments_reappear_as_width_grows():
    state = _busy_state()
    narrow = _text(render_status_bar(state, 50))
    wide = _text(render_status_bar(state, 300))
    assert len(wide) >= len(narrow)
    assert "42t/s" in wide
    # 300 cols comfortably fits everything the unbounded case does.
    assert "42t/s" in wide and "provider degraded" in wide


def test_quiet_state_is_unaffected_by_width_limit():
    """A session with nothing but the core segments active must render
    identically regardless of width, as long as width comfortably fits it."""
    state = StatusBarState(model_id="llama-3.3-70b", provider_id="groq")
    unbounded = _text(render_status_bar(state))
    bounded = _text(render_status_bar(state, 80))
    assert unbounded == bounded


def test_extremely_narrow_width_clips_model_id_with_ellipsis_not_mid_char_garbage():
    state = StatusBarState(
        model_id="a-very-long-model-identifier-that-will-not-fit", mode_label="NORMAL"
    )
    text = _text(render_status_bar(state, 30))
    assert len(text) <= 30
    assert "…" in text or len(state.model_id) <= 30
