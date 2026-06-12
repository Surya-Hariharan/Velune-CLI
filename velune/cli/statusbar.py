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


def render_status_bar(state: StatusBarState) -> FormattedText:
    parts: list[tuple[str, str]] = []

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
    parts.append((ctx_style, f"ctx {pct:.0f}%"))

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
