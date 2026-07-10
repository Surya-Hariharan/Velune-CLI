"""Bottom status bar for the Velune REPL.

A single low-noise line rendered via prompt_toolkit's bottom_toolbar showing
the live session state: active model, mode, context usage, and post-response
latency / throughput.  All values are read from a small mutable state object
the REPL updates as it works — rendering never probes hardware or providers.

Information hierarchy (left to right):
  exit hint  |  provider·model  |  mode  |  ctx bar  |  [branch]  |  [mcp]
  [bg jobs]  |  [alerts]  |  [provider issue]  |  [latency]  |  [throughput]

Removed vs. previous version:
  - workspace  (visible in the two-line prompt)
  - session cost  (use /stats instead)
  - retrieval note  (shown inline in the conversation)
  - provider ok  (normal state; only degraded/down are surfaced)
  - profile label  (hardware tier; rarely action-relevant)
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.formatted_text import FormattedText

from velune.cli import design

_BG = design.BACKGROUND
STATUS_BAR_STYLES: dict[str, str] = {
    "bottom-toolbar": f"noinherit bg:{_BG} {design.MUTED}",
    "bottom-toolbar.key": f"bg:{_BG} {design.FAINT}",
    "bottom-toolbar.model": f"bg:{_BG} {design.MUTED}",
    "bottom-toolbar.mode": f"bg:{_BG} {design.MUTED}",
    "bottom-toolbar.ok": f"bg:{_BG} {design.MUTED}",
    "bottom-toolbar.warn": f"bg:{_BG} {design.WARN}",
    "bottom-toolbar.danger": f"bg:{_BG} {design.DANGER}",
    "bottom-toolbar.hint": f"bg:{_BG} {design.MUTED} italic",
    "bottom-toolbar.speed": f"bg:{_BG} {design.FAINT}",
    "bottom-toolbar.privacy": f"bg:{_BG} {design.FAINT}",
    "bottom-toolbar.project": f"bg:{_BG} {design.FAINT}",
}

_SEP = ("class:bottom-toolbar.key", " │ ")


@dataclass
class StatusBarState:
    model_id: str | None = None
    mode_label: str = "NORMAL"
    profile_label: str | None = None  # kept for compat; no longer rendered
    context_pct: float = 0.0
    last_latency_ms: float | None = None
    last_tokens_per_sec: float | None = None
    retrieval_note: str | None = None  # kept for compat; shown inline now
    workspace_name: str | None = None  # kept for compat; shown in prompt
    exit_hint: bool = False
    context_used: int | None = None
    context_max: int | None = None
    session_cost: float = 0.0  # kept for compat; use /stats to view
    provider_health: str | None = None  # "ok" | "degraded" | "down"
    bg_job_count: int = 0
    alert_count: int = 0
    provider_id: str | None = None  # active model's provider (e.g. "groq")
    git_branch: str | None = None  # active branch; None/non-git stays silent
    mcp_connected: int = 0
    mcp_total: int = 0  # 0 = no servers configured, row stays silent


def _format_tokens(n: int) -> str:
    """Compact token count: 142000 → '142k', 1500 → '1.5k', 800 → '800'."""
    if n >= 1000:
        val = n / 1000
        return f"{val:.0f}k" if val >= 10 or val == int(val) else f"{val:.1f}k"
    return str(n)


def _context_bar(pct: float) -> str:
    """Visual context usage bar: ▮▮▮▯▯▯▯▯▯▯"""
    filled = int(pct / 10)
    return "▮" * filled + "▯" * (10 - filled)


def render_status_bar(state: StatusBarState) -> FormattedText:
    parts: list[tuple[str, str]] = []

    # Exit hint takes precedence over everything when active
    if state.exit_hint:
        parts.append(("class:bottom-toolbar.hint", " Ctrl+C again to exit"))
        parts.append(_SEP)

    # Provider + model — primary information
    if state.model_id:
        if state.provider_id:
            parts.append(("class:bottom-toolbar.key", f" {state.provider_id}·"))
            parts.append(("class:bottom-toolbar.model", state.model_id))
        else:
            parts.append(("class:bottom-toolbar.model", f" {state.model_id}"))
    else:
        parts.append(("class:bottom-toolbar.model", " no model"))

    parts.append(_SEP)

    # Mode — always shown (NORMAL is the common case but still orientation)
    parts.append(("class:bottom-toolbar.mode", state.mode_label))

    parts.append(_SEP)

    # Context usage with visual bar
    pct = state.context_pct
    if pct < 70:
        ctx_style = "class:bottom-toolbar.ok"
    elif pct < 90:
        ctx_style = "class:bottom-toolbar.warn"
    else:
        ctx_style = "class:bottom-toolbar.danger"

    ctx_bar = _context_bar(pct)
    if state.context_used is not None and state.context_max:
        ctx_label = (
            f"ctx {pct:.0f}%  "
            f"{_format_tokens(state.context_used)}/{_format_tokens(state.context_max)}"
        )
    else:
        ctx_label = f"ctx {pct:.0f}%"
    parts.append((ctx_style, f"{ctx_bar} {ctx_label}"))

    # Git branch — only inside a repository
    if state.git_branch and state.git_branch not in ("non-git", "unknown"):
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.project", state.git_branch))

    # MCP — only when servers are configured; degraded counts get warn color
    if state.mcp_total > 0:
        parts.append(_SEP)
        mcp_style = (
            "class:bottom-toolbar.ok"
            if state.mcp_connected == state.mcp_total
            else "class:bottom-toolbar.warn"
        )
        parts.append((mcp_style, f"mcp {state.mcp_connected}/{state.mcp_total}"))

    # Background jobs — only when running
    if state.bg_job_count > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.warn", f"bg:{state.bg_job_count}"))

    # Unread alerts — only when present
    if state.alert_count > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.warn", f"alerts:{state.alert_count}"))

    # Provider health — only when there is an issue ("ok" is silent)
    if state.provider_health == "degraded":
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.warn", "provider degraded"))
    elif state.provider_health == "down":
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.danger", "provider down"))

    # Latency — only after the first response of this session
    if state.last_latency_ms is not None:
        parts.append(_SEP)
        if state.last_latency_ms >= 1000:
            latency = f"{state.last_latency_ms / 1000:.1f}s"
        else:
            latency = f"{state.last_latency_ms:.0f}ms"
        parts.append(("class:bottom-toolbar.speed", latency))

    # Throughput — only when streaming and non-zero
    if state.last_tokens_per_sec is not None and state.last_tokens_per_sec > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar", f"{state.last_tokens_per_sec:.0f}t/s"))

    return FormattedText(parts)
