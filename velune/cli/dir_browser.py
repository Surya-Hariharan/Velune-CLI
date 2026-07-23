"""Interactive terminal-style directory browser for the Velune REPL.

A prompt_toolkit application that feels like navigating a shell: ↑↓ to move,
Enter to descend into a folder, ← (or selecting ``..``) to go up, type to
filter, and a pinned "Use this folder" row to choose the current directory.
At the top it lists mounted drives / volumes so users can hop to an external
SSD, USB stick, or secondary disk where their models actually live.

Used by ``/model locate`` to register a custom Ollama model store. The optional
``validate`` callback annotates each directory (e.g. "Ollama store") so the
user gets feedback *before* committing, and the chosen path is returned to the
caller for verification + persistence.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Callable
from pathlib import Path

from velune.cli.autocomplete import fuzzy_score

# Sentinel ids for the two special rows.
_USE = "\x00use"
_UP = "\x00up"


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


async def browse_for_directory(
    start: Path | None = None,
    *,
    title: str = "Select a folder",
    validate: Callable[[Path], bool] | None = None,
) -> Path | None:
    """Run the browser; return the chosen directory or None on cancel."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    current: list[Path | None] = [Path(start).expanduser() if start else Path.home()]
    selected_index = [0]
    filter_text = [""]
    result: list[Path | None] = [None]

    def _rows() -> list[tuple[str, str, str]]:
        """Build (id, label, meta) rows for the current location."""
        cur = current[0]
        rows: list[tuple[str, str, str]] = []
        if cur is None:
            # Drive / volume list.
            for d in _list_drives():
                rows.append((str(d), str(d), "drive"))
            return rows

        # Pinned actions for a real directory.
        meta = ""
        if validate is not None:
            try:
                meta = "valid Ollama store" if validate(cur) else "not an Ollama store"
            except Exception:
                meta = ""
        rows.append((_USE, "[ Use this folder ]", meta))
        rows.append((_UP, ".. (up)", ""))
        for child in _safe_listdir(cur):
            ann = ""
            if validate is not None:
                try:
                    if validate(child):
                        ann = "Ollama store"
                except Exception:
                    ann = ""
            rows.append((str(child), child.name + "/", ann))
        return rows

    def _visible() -> list[tuple[str, str, str]]:
        rows = _rows()
        if not filter_text[0]:
            return rows
        # Keep the pinned action rows; fuzzy-filter the rest by label.
        pinned = [r for r in rows if r[0] in (_USE, _UP)]
        rest = [r for r in rows if r[0] not in (_USE, _UP)]
        scored = [(fuzzy_score(filter_text[0], r[1]), r) for r in rest]
        filtered = [r for s, r in sorted(scored, key=lambda t: -t[0]) if s > 0]
        return pinned + filtered

    def _render() -> FormattedText:
        cur = current[0]
        visible = _visible()
        if visible:
            selected_index[0] = min(selected_index[0], len(visible) - 1)
        loc = "Drives / volumes" if cur is None else str(cur)
        lines: list[tuple[str, str]] = [
            ("bold", f"  {title}\n"),
            ("fg:ansicyan", f"  {loc}\n"),
            (
                "fg:ansibrightblack",
                "  (↑↓ move · Enter open · ← up · type to filter · Esc cancel)\n\n",
            ),
        ]
        if filter_text[0]:
            lines.append(("fg:ansicyan", f"  filter: {filter_text[0]}\n\n"))
        if not visible:
            lines.append(("fg:ansiyellow", "  (empty / no access)\n"))
        for i, (_id, label, meta) in enumerate(visible):
            is_sel = i == selected_index[0]
            prefix = "❯ " if is_sel else "  "
            style = "bold fg:cyan" if is_sel else ""
            if _id == _USE:
                style = "bold fg:ansigreen" if is_sel else "fg:ansigreen"
            lines.append((style, f"  {prefix}{label:<40}"))
            if meta:
                color = "fg:ansigreen" if meta.startswith("valid") else "fg:ansibrightblack"
                lines.append((color, f"  {meta}"))
            lines.append(("", "\n"))
        return FormattedText(lines)

    def _go_up() -> None:
        cur = current[0]
        if cur is None:
            return
        parent = cur.parent
        if parent == cur:
            # At a filesystem root — surface the drive/volume list.
            current[0] = None
        else:
            current[0] = parent
        filter_text[0] = ""
        selected_index[0] = 0

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:
        count = len(_visible())
        if count:
            selected_index[0] = (selected_index[0] - 1) % count

    @kb.add("down")
    def _down(event) -> None:
        count = len(_visible())
        if count:
            selected_index[0] = (selected_index[0] + 1) % count

    @kb.add("left")
    def _left(event) -> None:
        _go_up()

    # Mouse wheel — same convention as the main REPL transcript (fullscreen.py).
    @kb.add(Keys.ScrollUp, eager=True)
    def _scroll_up(event) -> None:
        _up(event)

    @kb.add(Keys.ScrollDown, eager=True)
    def _scroll_down(event) -> None:
        _down(event)

    @kb.add("enter")
    def _enter(event) -> None:
        visible = _visible()
        if not visible:
            return
        _id, _label, _meta = visible[selected_index[0]]
        if _id == _USE:
            result[0] = current[0]
            event.app.exit()
            return
        if _id == _UP:
            _go_up()
            return
        # Descend into the chosen directory (or drive root).
        current[0] = Path(_id)
        filter_text[0] = ""
        selected_index[0] = 0

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event) -> None:
        result[0] = None
        event.app.exit()

    @kb.add("backspace")
    def _backspace(event) -> None:
        if filter_text[0]:
            filter_text[0] = filter_text[0][:-1]
            selected_index[0] = 0
        else:
            _go_up()

    @kb.add("<any>")
    def _type(event) -> None:
        ch = event.data
        if ch and ch.isprintable():
            filter_text[0] += ch
            selected_index[0] = 0

    app = Application(
        layout=Layout(Window(content=FormattedTextControl(_render, focusable=True))),
        key_bindings=kb,
        full_screen=False,
        mouse_support=True,
    )
    await app.run_async()
    return result[0]
