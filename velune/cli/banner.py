"""Velune startup banner — minimal workspace summary.

Default mode: a compact 8-line surface showing wordmark + key-value pairs.
Splash mode:  set ``VELUNE_SPLASH=1`` to prepend the full ASCII wordmark.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.text import Text

from velune import __version__

# Big "VELUNE" wordmark in the ANSI Shadow figlet font. Each entry is one
# letter as six equal-height rows; a per-letter color list (built lazily in
# ``_render_logo`` from the brand palette) paints a left-to-right gradient
# across the word.  Only shown when VELUNE_SPLASH=1.
_LOGO_ART: list[list[str]] = [
    [  # V
        "██╗   ██╗",
        "██║   ██║",
        "██║   ██║",
        "╚██╗ ██╔╝",
        " ╚████╔╝ ",
        "  ╚═══╝  ",
    ],
    [  # E
        "███████╗",
        "██╔════╝",
        "█████╗  ",
        "██╔══╝  ",
        "███████╗",
        "╚══════╝",
    ],
    [  # L
        "██╗     ",
        "██║     ",
        "██║     ",
        "██║     ",
        "███████╗",
        "╚══════╝",
    ],
    [  # U
        "██╗   ██╗",
        "██║   ██║",
        "██║   ██║",
        "██║   ██║",
        "╚██████╔╝",
        " ╚═════╝ ",
    ],
    [  # N
        "███╗   ██╗",
        "████╗  ██║",
        "██╔██╗ ██║",
        "██║╚██╗██║",
        "██║ ╚████║",
        "╚═╝  ╚═══╝",
    ],
    [  # E
        "███████╗",
        "██╔════╝",
        "█████╗  ",
        "██╔══╝  ",
        "███████╗",
        "╚══════╝",
    ],
]


def _render_logo() -> Text:
    """Build the colored multi-line VELUNE wordmark as a single rich ``Text``."""
    from velune.cli import design

    colors = [
        design.ACCENT,  # V — hot pink
        design.ACCENT_SOFT,  # E — soft blush
        design.GREEN,  # L — rose
        design.GREEN,  # U — rose
        design.HIGHLIGHT,  # N — vivid magenta
        design.PRIMARY_GREEN,  # E — deep magenta
    ]
    logo = Text()
    rows = len(_LOGO_ART[0])
    for row in range(rows):
        for letter, color in zip(_LOGO_ART, colors, strict=False):
            logo.append(letter[row], style=color)
        if row < rows - 1:
            logo.append("\n")
    return logo


def _short_gpu_name(name: str) -> str:
    """Trim vendor/marketing noise so the GPU fits on one line."""
    cleaned = name
    for token in ("NVIDIA", "GeForce", "Laptop GPU", "Laptop", "GPU", "(R)", "(TM)"):
        cleaned = cleaned.replace(token, " ")
    cleaned = " ".join(cleaned.split())
    return cleaned or name


def _display_path(workspace_path: str) -> str:
    """Abbreviate the home directory to ``~`` for a tidier workspace line."""
    try:
        return "~/" + str(Path(workspace_path).resolve().relative_to(Path.home())).replace(
            "\\", "/"
        )
    except Exception:
        return workspace_path


def render_startup_banner(
    console: Console,
    hardware_profile,
    configured_providers: list[str],
    ollama_live: bool,
    workspace_path: str,
    active_model_id: str | None,
    version: str = __version__,
    project_type_name: str | None = None,
    runtime_profile_label: str | None = None,
) -> None:
    """Render the Velune welcome surface.

    Default: an 8-line minimal block — wordmark, tagline, then four key-value
    rows for workspace / provider / model / status.  Set ``VELUNE_SPLASH=1`` to
    prepend the full ASCII wordmark before the summary.
    """
    from rich.align import Align

    from velune.cli import design

    # --- Optional full-logo splash (VELUNE_SPLASH=1) --------------------------
    if os.environ.get("VELUNE_SPLASH") in ("1", "true", "yes"):
        panel_width = min(console.width, 88)
        console.print()
        console.print(Align.center(_render_logo(), width=panel_width))
        console.print()

    # --- Wordmark line --------------------------------------------------------
    console.print(
        Text.from_markup(
            f"[bold {design.ACCENT}]Velune[/bold {design.ACCENT}]"
            f"  [{design.FAINT}]v{version}[/{design.FAINT}]"
        )
    )
    console.print(
        Text.from_markup(f"[{design.FAINT}]Local-first AI orchestration[/{design.FAINT}]")
    )
    console.print()

    # --- Provider summary -----------------------------------------------------
    if configured_providers:
        primary = configured_providers[0].title()
        if len(configured_providers) > 1:
            provider_label = f"{primary}  [dim]+{len(configured_providers) - 1} more[/dim]"
        else:
            provider_label = primary
    else:
        provider_label = f"[{design.WARN}]none configured[/{design.WARN}]"

    # --- Workspace display ----------------------------------------------------
    folder = _display_path(workspace_path)
    if project_type_name and project_type_name != "Unknown":
        folder += f"  [dim]({project_type_name})[/dim]"

    # --- Status ---------------------------------------------------------------
    if configured_providers and active_model_id:
        status_text = f"[{design.OK}]Ready[/{design.OK}]"
    elif configured_providers:
        status_text = f"[{design.WARN}]No model selected[/{design.WARN}]"
    else:
        status_text = f"[{design.WARN}]Setup required[/{design.WARN}]"

    # --- Key-value block ------------------------------------------------------
    rows: list[tuple[str, str]] = [
        ("Workspace", folder),
        ("Provider", provider_label),
        ("Model", active_model_id if active_model_id else f"[{design.FAINT}]none[/{design.FAINT}]"),
        ("Status", status_text),
    ]

    label_w = max(len(k) for k, _ in rows)
    for label, value in rows:
        console.print(
            Text.from_markup(
                f"  [{design.FAINT}]{label:<{label_w}}[/{design.FAINT}]"
                f"  [{design.MUTED}]{value}[/{design.MUTED}]"
            )
        )

    # --- Hardware warnings (only when present; suggestions live in /doctor) ---
    if hardware_profile.warnings:
        console.print()
        for warning in hardware_profile.warnings:
            console.print(Text.from_markup(f"  [{design.WARN}]Warning: {warning}[/{design.WARN}]"))
        console.print(
            Text.from_markup(
                f"  [{design.FAINT}]Run [bold]/doctor[/bold] for full diagnostics[/{design.FAINT}]"
            )
        )

    console.print()
