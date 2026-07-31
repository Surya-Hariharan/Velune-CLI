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
    "bottom-toolbar.ctx-ok": f"bg:{_BG} {design.OK}",
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
    # Providers whose stored key the provider itself has rejected. Surfaced
    # here rather than printed, so a background re-verification never writes
    # over the user's transcript mid-session. Empty = row stays silent.
    invalid_keys: tuple[str, ...] = ()


def _format_tokens(n: int) -> str:
    """Compact token count: 142000 → '142k', 1500 → '1.5k', 800 → '800'."""
    if n >= 1000:
        val = n / 1000
        return f"{val:.0f}k" if val >= 10 or val == int(val) else f"{val:.1f}k"
    return str(n)


def _context_bar(pct: float) -> str:
    """Visual context usage bar: ███░░░░░░░"""
    filled = int(pct / 10)
    return "█" * filled + "░" * (10 - filled)


# No caller-supplied width (e.g. the existing unit tests, which never render
# this against a real terminal) means "don't truncate" — every segment fits
# comfortably within this many columns in practice.
_NO_LIMIT = 10_000


def _clip(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1].rstrip() + design.ICON_ELLIPSIS


def _group_width(group: list[tuple[str, str]]) -> int:
    return sum(len(t) for _s, t in group)


def _clamp_parts(parts: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """Hard safety net: truncate *parts* so their combined text never exceeds
    *limit*, cutting (with an ellipsis) inside whichever fragment overflows.

    The core segments below reserve room for each other heuristically (model
    text is clipped against an estimate of what mode/context will cost), which
    covers every realistic terminal width. This exists only to make the
    "never exceeds `limit`" guarantee unconditional — including pathological
    widths wide enough for barely a few characters, where the heuristic
    reservation can still be off by a few columns.
    """
    if limit <= 0:
        return []
    out: list[tuple[str, str]] = []
    used = 0
    for style, text in parts:
        if used >= limit:
            break
        room = limit - used
        if len(text) <= room:
            out.append((style, text))
            used += len(text)
        else:
            out.append((style, _clip(text, room)))
            used = limit
            break
    return out


def render_status_bar(state: StatusBarState, width: int | None = None) -> FormattedText:
    """Render the status bar, dropping lower-priority segments (never
    mid-word-clipping a segment) once *width* runs out.

    A terminal at a narrower effective column count (smaller font / higher
    "zoom", or just a smaller window) has less room for this line than a
    wider one — the previous version concatenated every active segment with
    no width awareness at all, so prompt_toolkit's own non-wrapping
    ``height=1`` window would hard-clip the tail mid-segment on any
    combination of active indicators that didn't fit. Segments are grouped
    with their leading separator and added in the priority order documented
    in the module docstring; once a group would overflow *width* it (and
    only it) is skipped — a later, shorter group can still fit and is still
    tried, so the bar shows as much as it has room for rather than stopping
    at the first miss.
    """
    limit = width if width and width > 0 else _NO_LIMIT

    # --- Core: exit hint, model/provider, mode, context bar -------------------
    # Always shown — these are the primary orientation signals, not optional
    # indicators, so instead of dropping them under extreme width pressure the
    # model id itself is clipped to fit (see the `model_room` guard below).
    core: list[tuple[str, str]] = []
    if state.exit_hint:
        core.append(("class:bottom-toolbar.hint", " Ctrl+C again to exit"))
        core.append(_SEP)

    if state.model_id:
        prefix = f" {state.provider_id}·" if state.provider_id else " "
        # Reserve room for the prefix and everything queued after the model
        # (mode + context bar, generously estimated) so a long model id
        # clips instead of forcing those off the end of a narrow terminal.
        model_room = max(6, limit - len(prefix) - 28)
        model_text = _clip(state.model_id, model_room)
        if state.provider_id:
            core.append(("class:bottom-toolbar.key", prefix))
            core.append(("class:bottom-toolbar.model", model_text))
        else:
            core.append(("class:bottom-toolbar.model", f" {model_text}"))
    else:
        core.append(("class:bottom-toolbar.model", " no model"))

    core.append(_SEP)
    core.append(("class:bottom-toolbar.mode", state.mode_label))
    core.append(_SEP)

    # Context usage with visual bar. Thresholds come from design.py so the
    # status bar, prompt badge, and /context command all agree.
    pct = state.context_pct
    ctx_style_by_state = {
        "ok": "class:bottom-toolbar.ctx-ok",
        "warn": "class:bottom-toolbar.warn",
        "danger": "class:bottom-toolbar.danger",
    }
    ctx_style = ctx_style_by_state[design.context_state(pct)]

    ctx_bar = _context_bar(pct)
    if state.context_used is not None and state.context_max:
        ctx_label = (
            f"ctx {pct:.0f}%  "
            f"{_format_tokens(state.context_used)}/{_format_tokens(state.context_max)}"
        )
    else:
        ctx_label = f"ctx {pct:.0f}%"
    core.append((ctx_style, f"{ctx_bar} {ctx_label}"))

    if _group_width(core) > limit:
        core = _clamp_parts(core, limit)

    parts = list(core)
    used = _group_width(core)

    # --- Optional segments, priority order (highest first) --------------------
    optional_groups: list[list[tuple[str, str]]] = []

    if state.git_branch and state.git_branch not in ("non-git", "unknown"):
        optional_groups.append([_SEP, ("class:bottom-toolbar.project", state.git_branch)])

    if state.mcp_total > 0:
        mcp_style = (
            "class:bottom-toolbar.ok"
            if state.mcp_connected == state.mcp_total
            else "class:bottom-toolbar.warn"
        )
        optional_groups.append([_SEP, (mcp_style, f"mcp {state.mcp_connected}/{state.mcp_total}")])

    if state.bg_job_count > 0:
        optional_groups.append([_SEP, ("class:bottom-toolbar.warn", f"bg:{state.bg_job_count}")])

    if state.alert_count > 0:
        optional_groups.append([_SEP, ("class:bottom-toolbar.warn", f"alerts:{state.alert_count}")])

    if state.invalid_keys:
        names = ", ".join(state.invalid_keys)
        optional_groups.append(
            [_SEP, ("class:bottom-toolbar.danger", f"{names} key invalid — /connect")]
        )

    if state.provider_health == "degraded":
        optional_groups.append([_SEP, ("class:bottom-toolbar.warn", "provider degraded")])
    elif state.provider_health == "down":
        optional_groups.append([_SEP, ("class:bottom-toolbar.danger", "provider down")])

    if state.last_latency_ms is not None:
        if state.last_latency_ms >= 1000:
            latency = f"{state.last_latency_ms / 1000:.1f}s"
        else:
            latency = f"{state.last_latency_ms:.0f}ms"
        optional_groups.append([_SEP, ("class:bottom-toolbar.speed", latency)])

    if state.last_tokens_per_sec is not None and state.last_tokens_per_sec > 0:
        optional_groups.append(
            [_SEP, ("class:bottom-toolbar", f"{state.last_tokens_per_sec:.0f}t/s")]
        )

    for group in optional_groups:
        group_len = _group_width(group)
        if used + group_len <= limit:
            parts.extend(group)
            used += group_len

    return FormattedText(parts)
