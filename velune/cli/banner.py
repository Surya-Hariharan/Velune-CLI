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

    # One hue per letter — a left-to-right pink gradient from hot pink through
    # blush and rose into vivid magenta.
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


def _user_name() -> str:
    """Best-effort display name for the welcome line.

    Prefers the configured git author name, then the OS login name. Falls back
    to a friendly generic so the banner never looks broken.
    """
    import getpass
    import subprocess

    try:
        name = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        if name:
            return name
    except Exception:
        pass
    try:
        return getpass.getuser()
    except Exception:
        return "there"


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
    """Render the Velune welcome surface in the Claude Code template style.

    A big pink ``VELUNE`` wordmark sits above one rounded, pink-bordered box.
    Inside, the box is split into two columns: the left shows the greeting,
    active model/profile and working folder; the right lists getting-started
    tips and a short "What's new" feed — exactly mirroring Claude Code's layout,
    re-skinned in the pink/white brand palette.
    """
    from rich.rule import Rule

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

    # --- Provider summary ------------------------------------------------
    active_providers = list(configured_providers)
    if active_providers:
        providers_value = f"{len(active_providers)} provider" + (
            "s" if len(active_providers) != 1 else ""
        )
    else:
        providers_value = "no providers"

    # --- Account / model line (Claude's "Opus 4.8 · Plan · email" row) ----
    model_label = active_model_id if active_model_id else "no model selected"
    profile_label = (runtime_profile_label or "auto").lower()
    account_line = f"[{design.MUTED}]{model_label}[/{design.MUTED}]"
    account_line += f" [dim]·[/dim] [{design.MUTED}]{profile_label}[/{design.MUTED}]"
    account_line += f" [dim]·[/dim] [{design.MUTED}]{providers_value}[/{design.MUTED}]"

    # --- Folder line -----------------------------------------------------
    folder_value = _display_path(workspace_path)
    if project_type_name and project_type_name != "Unknown":
        folder_value += f"  [dim]({project_type_name})[/dim]"

    # --- Left column: greeting + identity --------------------------------
    left = Group(
        Text.from_markup(f"[bold {design.ACCENT}]Welcome back, {_user_name()}![/]"),
        Text(""),
        Text.from_markup(account_line),
        Text(""),
        Text.from_markup(f"[{design.INFO}]{folder_value}[/{design.INFO}]"),
        Text.from_markup(f"[dim]{'  ·  '.join(machine_bits)}[/dim]"),
    )

    # --- Right column: tips + what's new ---------------------------------
    right = Group(
        Text.from_markup(f"[bold {design.WHITE}]Tips for getting started[/]"),
        Text.from_markup(
            f"[{design.MUTED}]1.[/] [{design.ACCENT}]/setup[/] "
            f"[{design.MUTED}]— configure providers & models[/]"
        ),
        Text.from_markup(
            f"[{design.MUTED}]2.[/] [{design.ACCENT}]/model connect[/] "
            f"[{design.MUTED}]— pick your default model[/]"
        ),
        Text.from_markup(
            f"[{design.MUTED}]3.[/] [{design.ACCENT}]/help[/] "
            f"[{design.MUTED}]— see every command[/]"
        ),
        Rule(style=design.FAINT),
        Text.from_markup(f"[bold {design.WHITE}]What's new[/]"),
        Text.from_markup(
            f"[{design.ACCENT_SOFT}]✦[/] [{design.MUTED}]Faster startup — "
            f"config & doctor are ~5–8× quicker[/]"
        ),
        Text.from_markup(
            f"[{design.ACCENT_SOFT}]✦[/] [{design.MUTED}]Lazy model discovery "
            f"trims the cold-start path[/]"
        ),
        Text.from_markup("[dim]/help for more[/dim]"),
    )

    # --- Two-column layout inside one rounded, pink-bordered box ----------
    grid = Table.grid(padding=(0, 4))
    grid.add_column(justify="left")  # identity
    grid.add_column(justify="left")  # tips / news
    grid.add_row(left, right)

    title = Text.from_markup(f"[bold {design.ACCENT}]✦ Velune CLI[/] [dim]v{version}[/dim]")

    panel_width = min(console.width, 88)
    subtitle = Text.from_markup(
        f"[{design.MUTED}]Local-first AI orchestration[/{design.MUTED}]"
        f"   [dim]·[/dim]   [{design.ACCENT_SOFT}]pink & white[/{design.ACCENT_SOFT}]"
    )

    # Big stylish pink VELUNE wordmark, centered above the welcome box.
    console.print()
    console.print(Align.center(_render_logo(), width=panel_width))
    console.print(Align.center(subtitle, width=panel_width))
    console.print(
        Panel(
            grid,
            title=title,
            title_align="left",
            border_style=design.ACCENT,
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
