"""Bottom status bar for the Velune REPL.

A single low-noise line rendered via prompt_toolkit's bottom_toolbar showing
the live session state: active model, mode, runtime profile, context usage,
and last-response latency/throughput. All values are read from a small
mutable state object the REPL updates as it works — rendering never probes
hardware or providers.

Uses the organic/natural design palette with earth tones.
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.formatted_text import FormattedText

# Status bar color scheme: dark forest background with teal/green accents
STATUS_BAR_STYLES: dict[str, str] = {
    "bottom-toolbar": "noinherit bg:#0f1817 #7a9999",  # dark forest bg, warm gray-teal text
    "bottom-toolbar.key": "bg:#0f1817 #5a7979",  # subtle separator
    "bottom-toolbar.model": "bg:#0f1817 #2db8a4 bold",  # teal for model (orchestration)
    "bottom-toolbar.mode": "bg:#0f1817 #d97e35",  # warm orange for modes (energy)
    "bottom-toolbar.ok": "bg:#0f1817 #2fb86a",  # forest green for success
    "bottom-toolbar.warn": "bg:#0f1817 #f5a94b",  # warm orange for warnings
    "bottom-toolbar.danger": "bg:#0f1817 #d94d4d",  # muted red for errors
    "bottom-toolbar.project": "bg:#0f1817 #5eb8d4",  # cooler teal for workspace info
    "bottom-toolbar.hint": "bg:#0f1817 #f5a94b bold",  # warm orange for exit hint
    "bottom-toolbar.privacy": "bg:#0f1817 #1f8659",  # deep forest green for privacy
    "bottom-toolbar.speed": "bg:#0f1817 #f5a94b",  # warm orange for speed
}

_SEP = ("class:bottom-toolbar.key", "  ◆  ")


@dataclass
class StatusBarState:
    model_id: str | None = None
    mode_label: str = "NORMAL"
    profile_label: str | None = None
    context_pct: float = 0.0
    last_latency_ms: float | None = None
    last_tokens_per_sec: float | None = None
    retrieval_note: str | None = None  # e.g. "3 memories" after a retrieval
    workspace_name: str | None = None  # active project workspace
    exit_hint: bool = False  # "press Ctrl+C again to exit" window is open
    # Runtime visibility (Phase 2). All default-off: a segment only renders once
    # the REPL has a real value for it, so unconfigured sessions stay quiet.
    context_used: int | None = None  # tokens consumed in the live conversation
    context_max: int | None = None  # active model's context window
    session_cost: float = 0.0  # cumulative $ this session
    provider_health: str | None = None  # "ok" | "degraded" | "down"
    bg_job_count: int = 0  # active background jobs from JobRegistry
    alert_count: int = 0  # unread proactive alerts from AlertStore


def _format_tokens(n: int) -> str:
    """Compact token count: 142000 -> '142k', 1500 -> '1.5k', 800 -> '800'."""
    if n >= 1000:
        val = n / 1000
        return f"{val:.0f}k" if val >= 10 or val == int(val) else f"{val:.1f}k"
    return str(n)


def _context_bar(pct: float) -> str:
    """Visual context usage indicator: ▯▯▯▯▮▮▮▮ format."""
    filled = int(pct / 10)  # 0-10 segments
    empty = 10 - filled
    return "▮" * filled + "▯" * empty


_PROVIDER_STYLES = {
    "ok": ("class:bottom-toolbar.ok", "🟢 provider ok"),
    "degraded": ("class:bottom-toolbar.warn", "🟡 provider degraded"),
    "down": ("class:bottom-toolbar.danger", "🔴 provider down"),
}


def render_status_bar(state: StatusBarState) -> FormattedText:
    parts: list[tuple[str, str]] = []

    if state.exit_hint:
        parts.append(("class:bottom-toolbar.hint", " ⟲ Ctrl+C again to exit"))
        parts.append(_SEP)

    if state.workspace_name:
        parts.append(("class:bottom-toolbar.project", f" 🏠 {state.workspace_name}"))
        parts.append(_SEP)

    # Active model with orchestration indicator
    if state.model_id:
        parts.append(("class:bottom-toolbar.model", f" ⚡ {state.model_id}"))
    else:
        parts.append(("class:bottom-toolbar.model", " ⚡ (no model)"))

    parts.append(_SEP)

    # Mode indicator with visual distinction
    mode_icon = "◆" if state.mode_label == "NORMAL" else "◇"
    parts.append(("class:bottom-toolbar.mode", f"{mode_icon} {state.mode_label}"))
    if state.profile_label:
        parts.append(("class:bottom-toolbar", f" ({state.profile_label})"))

    parts.append(_SEP)

    # Context usage with visual gradient
    pct = state.context_pct
    ctx_style = (
        "class:bottom-toolbar.ok"
        if pct < 70
        else "class:bottom-toolbar.warn"
        if pct < 90
        else "class:bottom-toolbar.danger"
    )

    # Show the underlying budget (142k / 200k) alongside the percentage
    if state.context_used is not None and state.context_max:
        ctx_label = (
            f"ctx {pct:.0f}%  "
            f"{_format_tokens(state.context_used)}/{_format_tokens(state.context_max)}"
        )
    else:
        ctx_label = f"ctx {pct:.0f}%"

    ctx_bar = _context_bar(pct)
    parts.append((ctx_style, f"{ctx_bar} {ctx_label}"))

    if state.session_cost > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar", f"💰 ${state.session_cost:.2f}"))

    if state.bg_job_count > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.warn", f"⚙ {state.bg_job_count} bg"))

    if state.alert_count > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.warn", f"⚠ {state.alert_count}"))

    if state.provider_health in _PROVIDER_STYLES:
        style, label = _PROVIDER_STYLES[state.provider_health]
        parts.append(_SEP)
        parts.append((style, label))

    # Speed indicator (first token latency)
    if state.last_latency_ms is not None:
        parts.append(_SEP)
        if state.last_latency_ms >= 1000:
            latency = f"{state.last_latency_ms / 1000:.1f}s"
        else:
            latency = f"{state.last_latency_ms:.0f}ms"
        parts.append(("class:bottom-toolbar.speed", f"⚡ {latency}"))

    # Throughput
    if state.last_tokens_per_sec is not None and state.last_tokens_per_sec > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar", f"{state.last_tokens_per_sec:.0f} tok/s"))

    # Retrieval indicator
    if state.retrieval_note:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar.privacy", f"🔒 {state.retrieval_note}"))

    return FormattedText(parts)
