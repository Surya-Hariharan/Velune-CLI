"""Bottom status bar for the Velune REPL.

A single low-noise line rendered via prompt_toolkit's bottom_toolbar showing
the live session state: active model, mode, runtime profile, context usage,
and last-response latency/throughput. All values are read from a small
mutable state object the REPL updates as it works — rendering never probes
hardware or providers.
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.formatted_text import FormattedText

STATUS_BAR_STYLES: dict[str, str] = {
    "bottom-toolbar": "noinherit bg:#1c1c28 #8a8a9a",
    "bottom-toolbar.key": "bg:#1c1c28 #6a6a7a",
    "bottom-toolbar.model": "bg:#1c1c28 #a78bfa",
    "bottom-toolbar.mode": "bg:#1c1c28 #d4af37",
    "bottom-toolbar.ok": "bg:#1c1c28 #5fd787",
    "bottom-toolbar.warn": "bg:#1c1c28 #ffaf00",
    "bottom-toolbar.danger": "bg:#1c1c28 #ff5f5f",
    "bottom-toolbar.project": "bg:#1c1c28 #5fd7ff",
    "bottom-toolbar.hint": "bg:#1c1c28 #ffaf00 bold",
}

_SEP = ("class:bottom-toolbar.key", "  │  ")


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


def _format_tokens(n: int) -> str:
    """Compact token count: 142000 -> '142k', 1500 -> '1.5k', 800 -> '800'."""
    if n >= 1000:
        val = n / 1000
        return f"{val:.0f}k" if val >= 10 or val == int(val) else f"{val:.1f}k"
    return str(n)


_PROVIDER_STYLES = {
    "ok": ("class:bottom-toolbar.ok", "● provider ok"),
    "degraded": ("class:bottom-toolbar.warn", "● provider degraded"),
    "down": ("class:bottom-toolbar.danger", "● provider down"),
}


def render_status_bar(state: StatusBarState) -> FormattedText:
    parts: list[tuple[str, str]] = []

    if state.exit_hint:
        parts.append(("class:bottom-toolbar.hint", " ^C again to exit"))
        parts.append(_SEP)

    if state.workspace_name:
        parts.append(("class:bottom-toolbar.project", f" ⌂ {state.workspace_name}"))
        parts.append(_SEP)

    model = state.model_id or "no model"
    parts.append(("class:bottom-toolbar.model", f" {model}"))

    parts.append(_SEP)
    parts.append(("class:bottom-toolbar.mode", state.mode_label))
    if state.profile_label:
        parts.append(("class:bottom-toolbar", f" · {state.profile_label}"))

    parts.append(_SEP)
    pct = state.context_pct
    ctx_style = (
        "class:bottom-toolbar.ok"
        if pct < 70
        else "class:bottom-toolbar.warn"
        if pct < 90
        else "class:bottom-toolbar.danger"
    )
    # Show the underlying budget (142k / 200k) alongside the percentage when
    # the REPL knows both — turns an abstract "71%" into a concrete headroom.
    if state.context_used is not None and state.context_max:
        ctx_label = (
            f"ctx {pct:.0f}%  "
            f"{_format_tokens(state.context_used)}/{_format_tokens(state.context_max)}"
        )
    else:
        ctx_label = f"ctx {pct:.0f}%"
    parts.append((ctx_style, ctx_label))

    if state.session_cost > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar", f"${state.session_cost:.2f}"))

    if state.provider_health in _PROVIDER_STYLES:
        style, label = _PROVIDER_STYLES[state.provider_health]
        parts.append(_SEP)
        parts.append((style, label))

    if state.last_latency_ms is not None:
        parts.append(_SEP)
        if state.last_latency_ms >= 1000:
            latency = f"{state.last_latency_ms / 1000:.1f}s"
        else:
            latency = f"{state.last_latency_ms:.0f}ms"
        parts.append(("class:bottom-toolbar", f"first token {latency}"))

    if state.last_tokens_per_sec is not None and state.last_tokens_per_sec > 0:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar", f"{state.last_tokens_per_sec:.0f} tok/s"))

    if state.retrieval_note:
        parts.append(_SEP)
        parts.append(("class:bottom-toolbar", state.retrieval_note))

    return FormattedText(parts)
