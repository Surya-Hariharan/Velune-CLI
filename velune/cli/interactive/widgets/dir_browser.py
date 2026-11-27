"""Interactive terminal-style directory browser widget.

Feels like navigating a shell: up-arrow-down-arrow to move, Enter to descend
into a folder, left-arrow (or selecting ``..``) to go up, type to filter, and
a pinned "Use this folder" row to choose the current directory. At the top of
the drive/volume list it shows mounted drives so users can hop to an external
SSD, USB stick, or secondary disk where their models actually live.

Used by ``/model locate`` to register a custom Ollama model store. The
optional ``validate`` callback annotates each directory (e.g. "Ollama
store") so the user gets feedback *before* committing, and the chosen path
is submitted as the widget's result.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from velune.cli import design
from velune.cli.autocomplete import fuzzy_score
from velune.cli.interactive.widget import Widget

# Sentinel ids for the two pinned rows.
USE = "\x00use"
UP = "\x00up"


def _list_drives() -> list[Path]:
    """Return mounted drive / volume roots for this platform."""
    drives: list[Path] = []
    try:
        import psutil

        for part in psutil.disk_partitions(all=False):
            if part.mountpoint:
                drives.append(Path(part.mountpoint))
    except Exception:
        pass

    if platform.system() == "Windows":
        # Fallback / supplement: probe drive letters directly.
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            if root.exists() and root not in drives:
                drives.append(root)
    else:
        for base in ("/", "/mnt", "/media", "/Volumes", f"/media/{os.environ.get('USER', '')}"):
            p = Path(base)
            if p.exists() and p not in drives:
                drives.append(p)

    # De-dup while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for d in drives:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _safe_listdir(path: Path) -> list[Path]:
    """Sorted sub-directories of *path*, hidden ones last; never raises."""
    try:
        entries = [p for p in path.iterdir() if p.is_dir()]
    except (PermissionError, OSError):
        return []
    entries.sort(key=lambda p: (p.name.startswith("."), p.name.lower()))
    return entries


def parent_of(path: Path) -> Path | None:
    """The folder above *path*; ``None`` (the drive list) from a filesystem root."""
    return None if path.parent == path else path.parent


def browse_rows(
    current: Path | None, validate: Callable[[Path], bool] | None = None
) -> list[tuple[str, str, str]]:
    """``(id, label, meta)`` rows for *current*, or the drive list when it is None.

    Shared by the standalone widget and the REPL's palette browser
    (``interactive.browse_directory``), so both list the same rows.
    """

    def _check(path: Path) -> bool | None:
        if validate is None:
            return None
        try:
            return validate(path)
        except Exception:
            return None

    if current is None:
        return [(str(d), str(d), "drive") for d in _list_drives()]
    ok = _check(current)
    meta = "" if ok is None else ("valid Ollama store" if ok else "not an Ollama store")
    rows = [(USE, "[ Use this folder ]", meta), (UP, ".. (up)", "")]
    for child in _safe_listdir(current):
        rows.append((str(child), child.name + "/", "Ollama store" if _check(child) else ""))
    return rows


@dataclass(kw_only=True)
class DirBrowserWidget(Widget[Path]):
    """Navigable directory tree. Submits the chosen ``Path`` on "Use this folder"."""

    title: str = "Select a folder"
    start: Path | None = None
    validate: Callable[[Path], bool] | None = None

    _current: Path | None = field(default=None, init=False)
    _index: int = field(default=0, init=False)
    _filter: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._current = Path(self.start).expanduser() if self.start else Path.home()

    # -- rows ---------------------------------------------------------------

    def _rows(self) -> list[tuple[str, str, str]]:
        return browse_rows(self._current, self.validate)

    def _visible(self) -> list[tuple[str, str, str]]:
        rows = self._rows()
        if not self._filter:
            return rows
        # Keep the pinned action rows; fuzzy-filter the rest by label.
        pinned = [r for r in rows if r[0] in (USE, UP)]
        rest = [r for r in rows if r[0] not in (USE, UP)]
        scored = [(fuzzy_score(self._filter, r[1]), r) for r in rest]
        filtered = [r for s, r in sorted(scored, key=lambda t: -t[0]) if s > 0]
        return pinned + filtered

    # -- driving the widget ---------------------------------------------------

    def move(self, delta: int) -> None:
        visible = self._visible()
        if not visible:
            return
        self._index = (self._index + delta) % len(visible)

    def type_char(self, char: str) -> bool:
        if not char or not char.isprintable():
            return False
        self._filter += char
        self._index = 0
        return True

    def backspace(self) -> None:
        if self._filter:
            self._filter = self._filter[:-1]
            self._index = 0
        else:
            self.go_up()

    def go_up(self) -> None:
        if self._current is None:
            return
        self._current = parent_of(self._current)
        self._filter = ""
        self._index = 0

    def submit(self) -> None:
        visible = self._visible()
        if not visible:
            return
        _id, _label, _meta = visible[self._index % len(visible)]
        if _id == USE:
            self.on_submit(self._current)
            return
        if _id == UP:
            self.go_up()
            return
        # Descend into the chosen directory (or drive root).
        self._current = Path(_id)
        self._filter = ""
        self._index = 0

    # -- rendering -----------------------------------------------------------

    def render(self) -> StyleAndTextTuples:
        cur = self._current
        visible = self._visible()
        if visible:
            self._index = min(self._index, len(visible) - 1)
        loc = "Drives / volumes" if cur is None else str(cur)

        lines: StyleAndTextTuples = [
            (f"bold fg:{design.ACCENT}", f"  {self.title}\n"),
            (f"fg:{design.INFO}", f"  {loc}\n"),
        ]
        lines.append(("", "\n"))

        if self._filter:
            lines.append((f"fg:{design.INFO}", f"  filter: {self._filter}\n\n"))

        if not visible:
            lines.append((f"fg:{design.WARN}", "  (empty / no access)\n"))

        for i, (_id, label, meta) in enumerate(visible):
            is_sel = i == self._index
            prefix = "❯ " if is_sel else "  "
            if _id == USE:
                row_style = f"bold fg:{design.OK}" if is_sel else f"fg:{design.OK}"
            else:
                row_style = f"bold fg:{design.ACCENT}" if is_sel else f"fg:{design.WHITE}"
            lines.append((row_style, f"  {prefix}{label:<40}"))
            if meta:
                meta_color = design.OK if meta.startswith("valid") else design.MUTED
                lines.append((f"fg:{meta_color}", f"  {meta}"))
            lines.append(("", "\n"))

        return lines

    # -- key bindings ----------------------------------------------------

    def key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("up")
        def _up(event) -> None:
            self.move(-1)

        @kb.add("down")
        def _down(event) -> None:
            self.move(1)

        @kb.add("left")
        def _left(event) -> None:
            self.go_up()

        @kb.add(Keys.ScrollUp, eager=True)
        def _scroll_up(event) -> None:
            self.move(-1)

        @kb.add(Keys.ScrollDown, eager=True)
        def _scroll_down(event) -> None:
            self.move(1)

        @kb.add("enter")
        def _enter(event) -> None:
            self.submit()

        @kb.add("backspace")
        def _bs(event) -> None:
            self.backspace()

        @kb.add("<any>")
        def _type(event) -> None:
            self.type_char(event.data)

        return kb

    def footer_hint(self) -> str:
        return "↑↓ move  ·  Enter open  ·  ← up  ·  type to filter  ·  Esc cancel"
