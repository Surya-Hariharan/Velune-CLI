from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from velune import __version__

# Big "VELUNE" wordmark in the ANSI Shadow figlet font. Each entry is one
# letter as six equal-height rows; a per-letter color list (built lazily in
# ``_render_logo`` from the brand palette) paints a left-to-right
# teal → green → orange gradient across the word.
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

    # One hue per letter — a warm progression from brand teal to warm orange.
    colors = [
        design.ACCENT,  # V — deep teal
        design.ACCENT_SOFT,  # E — soft teal
        design.GREEN,  # L — forest green
        design.GREEN,  # U — forest green
        design.ENERGY,  # N — golden orange
        design.HIGHLIGHT,  # E — warm orange
    ]
    logo = Text()
    rows = len(_LOGO_ART[0])
    for row in range(rows):
        for letter, color in zip(_LOGO_ART, colors):
            logo.append(letter[row], style=color)
        if row < rows - 1:
            logo.append("\n")
    return logo


def _short_gpu_name(name: str) -> str:
    """Trim vendor/marketing noise so the GPU fits on one line.

    ``NVIDIA GeForce RTX 4060 Laptop GPU`` -> ``RTX 4060``.
    """
    cleaned = name
    for token in ("NVIDIA", "GeForce", "Laptop GPU", "Laptop", "GPU", "(R)", "(TM)"):
        cleaned = cleaned.replace(token, " ")
    cleaned = " ".join(cleaned.split())
    return cleaned or name


def _display_path(workspace_path: str) -> str:
    """Abbreviate the user's home directory to ``~`` for a tidier folder line."""
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
    """Render a compact, single-panel welcome — calm and scannable.

    Inspired by Claude Code's welcome surface: one rounded box, aligned
    label/value rows, and an action hint per row. Heavy ASCII art and wide
    separator rules are intentionally avoided so the box reads as a quiet
    header rather than a splash screen.
    """
    from velune.cli import design

    # --- Machine summary -------------------------------------------------
    vram_gb = hardware_profile.vram_total_gb
    if hardware_profile.gpu_name and vram_gb is not None:
        gpu_part = f"{_short_gpu_name(hardware_profile.gpu_name)} {vram_gb:.0f}GB"
    elif hardware_profile.gpu_name:
        gpu_part = _short_gpu_name(hardware_profile.gpu_name)
    else:
        gpu_part = "CPU only"
    machine_bits = [f"{hardware_profile.total_ram_gb:.0f}GB RAM", gpu_part]
    if runtime_profile_label:
        machine_bits.append(runtime_profile_label.lower())

    # --- Provider summary ------------------------------------------------
    provider_chunks: list[str] = []
    if ollama_live:
        provider_chunks.append(f"[{design.GREEN}]◆ ollama[/{design.GREEN}]")
    for pid in configured_providers:
        if pid != "ollama":
            provider_chunks.append(f"[{design.ACCENT}]◆ {pid}[/{design.ACCENT}]")
    providers_value = (
        "  ".join(provider_chunks)
        if provider_chunks
        else f"[{design.WARN}]none configured[/{design.WARN}]"
    )

    # --- Folder line -----------------------------------------------------
    folder_value = _display_path(workspace_path)
    if project_type_name and project_type_name != "Unknown":
        folder_value += f"  [dim]({project_type_name})[/dim]"

    # --- Aligned label/value/hint grid ----------------------------------
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=design.MUTED, justify="left", no_wrap=True)  # label
    grid.add_column(justify="left")  # value
    grid.add_column(style="dim", justify="left", no_wrap=True)  # hint

    if active_model_id:
        model_value = f"[{design.ACCENT}]{active_model_id}[/{design.ACCENT}]"
    else:
        model_value = f"[{design.WARN}]none selected[/{design.WARN}]"
    grid.add_row("Model", model_value, "/model")
    grid.add_row("Provider", providers_value, "/status")
    grid.add_row("Machine", f"[dim]{'  ·  '.join(machine_bits)}[/dim]", "")
    grid.add_row("Folder", f"[{design.INFO}]{folder_value}[/{design.INFO}]", "")

    footer = Text.from_markup(
        f"[dim]/help[/dim] [{design.MUTED}]commands[/{design.MUTED}]   "
        f"[dim]·[/dim]   [dim]/setup[/dim] [{design.MUTED}]providers[/{design.MUTED}]   "
        f"[dim]·[/dim]   [dim]/model[/dim] [{design.MUTED}]connect[/{design.MUTED}]"
    )

    body = Group(
        grid,
        Text(""),
        footer,
    )

    subtitle = Text.from_markup(
        f"[{design.MUTED}]Local-first AI orchestration[/{design.MUTED}]"
        f"   [dim]·[/dim]   [dim]v{version}[/dim]"
    )

    panel_width = min(console.width, 72)

    # Big VELUNE wordmark + tagline, centered above the info panel.
    console.print()
    console.print(Align.center(_render_logo(), width=panel_width))
    console.print(Align.center(subtitle, width=panel_width))
    console.print(
        Panel(
            body,
            border_style=design.FAINT,
            box=box.ROUNDED,
            padding=(1, 3),
            width=panel_width,
        )
    )

    # Warnings/suggestions live *outside* the box so the header stays clean and
    # they only appear when the machine actually needs guidance.
    if hardware_profile.warnings or hardware_profile.suggestions:
        for warning in hardware_profile.warnings:
            console.print(f"  [{design.WARN}]⚠ {warning}[/{design.WARN}]")
        for suggestion in hardware_profile.suggestions:
            console.print(f"  [{design.INFO}]→ {suggestion}[/{design.INFO}]")
    console.print()
