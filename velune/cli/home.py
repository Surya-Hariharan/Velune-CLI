"""Home surface for the fullscreen REPL — compact header + runtime summary.

Replaces the old full-screen centered wordmark: instead of branding, the empty
transcript shows a small VELUNE header in the upper-left corner followed by a
key-value block of live runtime facts (repository, memory, MCP, providers,
local runtimes). The prompt box below remains the visual focus.

The renderer is a pure function over :class:`HomeState` so it can be tested
without a terminal. All values are supplied by the REPL — nothing here probes
providers, git, or the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText

from velune.cli import design

_MARGIN = "  "
_LABEL_WIDTH = 12

HOME_STYLES: dict[str, str] = {
    "home.brand": f"bg:{design.BACKGROUND} {design.ACCENT} bold",
    "home.version": f"bg:{design.BACKGROUND} {design.FAINT}",
    "home.model": f"bg:{design.BACKGROUND} {design.WHITE}",
    "home.meta": f"bg:{design.BACKGROUND} {design.MUTED}",
    "home.path": f"bg:{design.BACKGROUND} {design.SECONDARY}",
    "home.label": f"bg:{design.BACKGROUND} {design.FAINT}",
    "home.value": f"bg:{design.BACKGROUND} {design.SECONDARY}",
    "home.ok": f"bg:{design.BACKGROUND} {design.OK}",
    "home.warn": f"bg:{design.BACKGROUND} {design.WARN}",
    "home.hint": f"bg:{design.BACKGROUND} {design.FAINT} italic",
}


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


def _info_rows(state: HomeState) -> list[tuple[str, str, str]]:
    """(label, value, value_style) rows — only rows that carry signal."""
    rows: list[tuple[str, str, str]] = []

    # Repository — summarize briefly when one is open.
    if state.git_branch or state.project_type or state.indexed_files is not None:
        parts: list[str] = []
        if state.project_type and state.project_type != "Unknown":
            parts.append(state.project_type)
        if state.indexed_files is not None:
            parts.append(f"{state.indexed_files} files indexed")
        else:
            parts.append("not indexed yet")
        rows.append(("Repository", " · ".join(parts), "class:home.value"))
    else:
        rows.append(("Repository", "none detected", "class:home.label"))

    if state.memory_label:
        rows.append(("Memory", state.memory_label, "class:home.value"))
    else:
        rows.append(("Memory", "empty", "class:home.label"))

    if state.mcp_total > 0:
        style = "class:home.value" if state.mcp_connected else "class:home.warn"
        rows.append(("MCP", f"{state.mcp_connected}/{state.mcp_total} servers connected", style))
    else:
        rows.append(("MCP", "no servers configured", "class:home.label"))

    if state.providers:
        primary = state.providers[0].title()
        label = (
            primary
            if len(state.providers) == 1
            else (f"{primary} +{len(state.providers) - 1} more")
        )
        rows.append(("Providers", label, "class:home.value"))
    else:
        rows.append(("Providers", "none — run /setup", "class:home.warn"))

    if state.local_runtime:
        rows.append(("Local", state.local_runtime, "class:home.value"))

    return rows


def render_home(state: HomeState, width: int) -> FormattedText:
    """Render the compact home surface as prompt_toolkit fragments."""
    usable = max(20, width) - len(_MARGIN)
    frags: list[tuple[str, str]] = []

    def line(*segments: tuple[str, str]) -> None:
        frags.append(("", _MARGIN))
        frags.extend(segments)
        frags.append(("", "\n"))

    def blank() -> None:
        frags.append(("", "\n"))

    blank()

    # --- Header: identity in three tight lines --------------------------------
    line(
        ("class:home.brand", "VELUNE CLI"),
        ("class:home.version", f"  v{state.version}" if state.version else ""),
    )
    if state.model_id:
        model = _clip(state.model_id, usable - 20)
        segments = [("class:home.model", model)]
        if state.provider:
            segments.append(("class:home.meta", f" {design.ICON_BULLET} {state.provider.title()}"))
        line(*segments)
    else:
        line(("class:home.warn", "no model selected"), ("class:home.meta", " — /model to choose"))

    path = display_path(state.workspace_path) if state.workspace_path else ""
    if path:
        segments = [("class:home.path", _clip(path, usable - 16))]
        if state.git_branch and state.git_branch not in ("unknown", "non-git"):
            segments.append(("class:home.meta", f" {design.ICON_BULLET} {state.git_branch}"))
        line(*segments)

    blank()

    # --- Runtime summary -------------------------------------------------------
    for label, value, style in _info_rows(state):
        line(
            ("class:home.label", f"{label:<{_LABEL_WIDTH}}"),
            (style, _clip(value, usable - _LABEL_WIDTH)),
        )

    blank()
    hint = "/ commands  ·  @file to mention  ·  Tab complete  ·  /help"
    if len(hint) > usable:
        hint = "/ commands  ·  /help"
    line(("class:home.hint", _clip(hint, usable)))

    return FormattedText(frags)
