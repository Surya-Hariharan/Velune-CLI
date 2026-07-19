"""Home surface for the fullscreen REPL — gradient wordmark + runtime summary.

The empty transcript shows a bold VELUNE wordmark painted with the brand
gradient (violet → blue → teal), a tagline + version, a compact block of live
runtime facts (model, repository, memory, MCP, providers), and a one-line hint.
On narrow terminals the block wordmark is swapped for a compact lockup so the
surface never overflows.

The renderer is a pure function over :class:`HomeState` so it can be tested
without a terminal. All values are supplied by the REPL — nothing here probes
providers, git, or the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText

from velune.cli import design

_MARGIN = "   "  # 3-space left gutter for the whole surface
_DOT = "●"
_DOT_OFF = "○"

HOME_STYLES: dict[str, str] = {
    "home.brand": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
    "home.tagline": f"bg:{design.BACKGROUND} {design.SECONDARY}",
    "home.version": f"bg:{design.BACKGROUND} {design.FAINT}",
    "home.model": f"bg:{design.BACKGROUND} {design.WHITE}",
    "home.meta": f"bg:{design.BACKGROUND} {design.MUTED}",
    "home.path": f"bg:{design.BACKGROUND} {design.SECONDARY}",
    "home.dot": f"bg:{design.BACKGROUND} {design.ACCENT}",
    "home.dot.off": f"bg:{design.BACKGROUND} {design.FAINT}",
    "home.value": f"bg:{design.BACKGROUND} {design.SECONDARY}",
    "home.label": f"bg:{design.BACKGROUND} {design.FAINT}",
    "home.ok": f"bg:{design.BACKGROUND} {design.OK}",
    "home.warn": f"bg:{design.BACKGROUND} {design.WARN}",
    "home.hint": f"bg:{design.BACKGROUND} {design.FAINT} italic",
    "home.hint.key": f"bg:{design.BACKGROUND} {design.ACCENT_SOFT}",
}

# --- Wordmark ---------------------------------------------------------------
# "VELUNE" in the ANSI-Shadow figlet style. All glyphs are single display-width
# (full-block + box-drawing), so character count equals rendered columns.
_WORDMARK_ROWS: tuple[str, ...] = (
    "██╗   ██╗███████╗██╗     ██╗   ██╗███╗   ██╗███████╗",
    "██║   ██║██╔════╝██║     ██║   ██║████╗  ██║██╔════╝",
    "██║   ██║█████╗  ██║     ██║   ██║██╔██╗ ██║█████╗  ",
    "╚██╗ ██╔╝██╔══╝  ██║     ██║   ██║██║╚██╗██║██╔══╝  ",
    " ╚████╔╝ ███████╗███████╗╚██████╔╝██║ ╚████║███████╗",
    "  ╚═══╝  ╚══════╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝",
)
_WORDMARK_WIDTH = max(len(r) for r in _WORDMARK_ROWS)


@dataclass
class HomeState:
    """Everything the home surface shows. Built by the REPL, rendered here."""

    version: str = ""
    model_id: str | None = None
    provider: str | None = None
    workspace_path: str = ""
    git_branch: str | None = None
    project_type: str | None = None
    indexed_files: int | None = None  # None = no index yet
    memory_label: str | None = None  # e.g. "cognitive 76.4 MB"
    mcp_connected: int = 0
    mcp_total: int = 0
    providers: tuple[str, ...] = field(default_factory=tuple)
    local_runtime: str | None = None  # e.g. "Ollama" when configured


def display_path(workspace_path: str) -> str:
    """Abbreviate the home directory to ``~`` for a tidier workspace line."""
    try:
        return "~/" + str(Path(workspace_path).resolve().relative_to(Path.home())).replace(
            "\\", "/"
        )
    except Exception:
        return workspace_path


def _clip(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1].rstrip() + design.ICON_ELLIPSIS


def _fit_segments(segments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """Trim a styled fragment run so its total visible length never exceeds
    *width*, truncating (with an ellipsis) inside the fragment that overflows."""
    out: list[tuple[str, str]] = []
    used = 0
    for style, text in segments:
        if used >= width:
            break
        if used + len(text) <= width:
            out.append((style, text))
            used += len(text)
        else:
            remaining = width - used
            clipped = _clip(text, remaining) if remaining > 1 else text[:remaining]
            out.append((style, clipped))
            break
    return out


def _wordmark_fragments(indent: str) -> list[tuple[str, str]]:
    """The gradient-painted block wordmark, one fragment-run per color change.

    Each column is assigned a color from the brand gradient by its horizontal
    position, so the whole wordmark reads as a single left-to-right sweep from
    violet through blue to teal. Space cells carry no color (background only).
    """
    frags: list[tuple[str, str]] = []
    denom = max(1, _WORDMARK_WIDTH - 1)
    # Precompute one color per column so every row shares the same vertical hue.
    col_style = [
        f"bg:{design.BACKGROUND} {design.gradient_hex(x / denom)} bold" for x in range(_WORDMARK_WIDTH)
    ]
    bg_style = f"bg:{design.BACKGROUND}"
    for row in _WORDMARK_ROWS:
        frags.append(("", indent))
        padded = row.ljust(_WORDMARK_WIDTH)
        cur_style: str | None = None
        buf = ""
        for x, ch in enumerate(padded):
            style = bg_style if ch == " " else col_style[x]
            if style != cur_style:
                if buf:
                    frags.append((cur_style or "", buf))
                cur_style = style
                buf = ch
            else:
                buf += ch
        if buf:
            frags.append((cur_style or "", buf))
        frags.append(("", "\n"))
    return frags


def _info_lines(state: HomeState, usable: int) -> list[list[tuple[str, str]]]:
    """Dot-prefixed runtime fact lines — only rows that carry signal."""
    lines: list[list[tuple[str, str]]] = []

    def dot(on: bool = True) -> tuple[str, str]:
        return ("class:home.dot", f"{_DOT}  ") if on else ("class:home.dot.off", f"{_DOT_OFF}  ")

    # Model · provider
    if state.model_id:
        segs = [dot(), ("class:home.model", _clip(state.model_id, usable - 24))]
        if state.provider:
            segs.append(("class:home.meta", f"  {design.ICON_BULLET} {state.provider.title()}"))
        lines.append(segs)
    else:
        lines.append(
            [dot(False), ("class:home.warn", "no model selected"), ("class:home.meta", "  — /model")]
        )

    # Repository: path · branch · type · files
    path = display_path(state.workspace_path) if state.workspace_path else ""
    if path or state.project_type or state.indexed_files is not None:
        meta: list[str] = []
        if state.git_branch and state.git_branch not in ("unknown", "non-git"):
            meta.append(f"{design.ICON_BRANCH} {state.git_branch}")
        if state.project_type and state.project_type != "Unknown":
            meta.append(state.project_type)
        meta.append(
            f"{state.indexed_files} files" if state.indexed_files is not None else "not indexed"
        )
        segs = [dot(), ("class:home.path", _clip(path or "workspace", usable - 34))]
        if meta:
            segs.append(("class:home.meta", "  " + f" {design.ICON_BULLET} ".join(meta)))
        lines.append(segs)

    # System: memory · MCP · providers
    sys_parts: list[tuple[str, str]] = []
    if state.memory_label:
        sys_parts.append(("class:home.value", state.memory_label))
    if state.mcp_total > 0:
        mcp_style = "class:home.value" if state.mcp_connected else "class:home.warn"
        sys_parts.append((mcp_style, f"MCP {state.mcp_connected}/{state.mcp_total}"))
    if state.providers:
        primary = state.providers[0].title()
        label = primary if len(state.providers) == 1 else f"{primary} +{len(state.providers) - 1}"
        sys_parts.append(("class:home.value", label))
    if state.local_runtime:
        sys_parts.append(("class:home.value", state.local_runtime))

    if sys_parts:
        segs = [dot()]
        for i, part in enumerate(sys_parts):
            if i:
                segs.append(("class:home.meta", f"  {design.ICON_BULLET}  "))
            segs.append(part)
        lines.append(segs)
    elif not state.providers:
        lines.append(
            [dot(False), ("class:home.warn", "no providers configured"), ("class:home.meta", "  — /setup")]
        )

    return lines


def render_home(state: HomeState, width: int) -> FormattedText:
    """Render the gradient home surface as prompt_toolkit fragments."""
    usable = max(20, width) - len(_MARGIN)
    frags: list[tuple[str, str]] = []

    def line(*segments: tuple[str, str]) -> None:
        frags.append(("", _MARGIN))
        frags.extend(segments)
        frags.append(("", "\n"))

    def blank() -> None:
        frags.append(("", "\n"))

    blank()

    # --- Wordmark: block gradient banner, or compact lockup when narrow -------
    if usable >= _WORDMARK_WIDTH:
        frags.extend(_wordmark_fragments(_MARGIN))
        blank()
        tagline = "Local-first multi-model AI developer CLI"
        segs = [("class:home.tagline", _clip(tagline, usable - 10))]
        if state.version:
            segs.append(("class:home.version", f"   v{state.version}"))
        line(*segs)
    else:
        # Compact lockup: gradient diamond + wordmark text on one line.
        segs = [
            ("class:home.dot", f"{design.ICON_DIAMOND} "),
            ("class:home.brand", "VELUNE"),
        ]
        if state.version:
            segs.append(("class:home.version", f"  v{state.version}"))
        line(*segs)
        line(("class:home.tagline", _clip("ai developer console", usable)))

    blank()

    # --- Runtime facts --------------------------------------------------------
    for segs in _info_lines(state, usable):
        line(*_fit_segments(segs, usable))

    blank()

    # --- Hint -----------------------------------------------------------------
    hint_parts = [
        ("class:home.hint.key", "/"),
        ("class:home.hint", " commands   "),
        ("class:home.hint.key", "@file"),
        ("class:home.hint", " to mention   "),
        ("class:home.hint.key", "Tab"),
        ("class:home.hint", " complete   "),
        ("class:home.hint.key", "/help"),
    ]
    hint_len = sum(len(t) for _s, t in hint_parts)
    if hint_len <= usable:
        line(*hint_parts)
    else:
        line(("class:home.hint.key", "/"), ("class:home.hint", " commands   "),
             ("class:home.hint.key", "/help"))

    return FormattedText(frags)
