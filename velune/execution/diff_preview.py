"""Diff preview system — renders unified diffs and prompts for user approval
before any agent-driven file write is committed to disk."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level auto-accept flag.  Set to True when the user passes --yes.
# ---------------------------------------------------------------------------
_auto_accept: bool = False


def configure(auto_accept: bool) -> None:
    """Set the module-wide auto-accept flag (called from app.py --yes)."""
    global _auto_accept
    _auto_accept = auto_accept


def set_auto_accept(auto_accept: bool) -> None:
    """Backward-compatible alias for tests and older integrations."""
    configure(auto_accept)


def is_auto_accept() -> bool:
    return _auto_accept


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


class DiffDecision(Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"


@dataclass
class FileDiff:
    path: Path
    original: str  # Empty string if the file is new
    proposed: str
    is_new_file: bool
    is_deletion: bool


def _lf(text: str) -> str:
    """*text* with every line ending (CRLF, lone CR) as LF — the form ``read_text`` yields."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_for_preview(path: Path) -> str:
    """The file's text for display, decoded as ``Path.read_text(errors="replace")`` would.

    At most the editing limit (+1 byte) is read: a larger file raises
    :class:`~velune.tools.filesystem.mutation.FileTooLargeError` instead of being loaded
    whole just to draw an approval preview.
    """
    import io

    from velune.tools.filesystem.mutation import MAX_MUTATION_BYTES, read_bounded

    raw = read_bounded(path, MAX_MUTATION_BYTES)
    return io.TextIOWrapper(io.BytesIO(raw), errors="replace").read()


def compute_file_diff(path: Path, proposed: str) -> FileDiff:
    """Build a FileDiff against the current on-disk state (no console needed).

    The original is read with universal newlines (every line ending becomes LF), but a
    proposal built from a CRLF file keeps its CRLFs; compared as they are, *every* line
    would differ and the approval diff would show the whole file as rewritten. Both sides
    are compared with LF endings. This is for display and comparison only: the tools that
    write the file keep its real encoding and line endings (``velune.tools.filesystem``).
    A new file has nothing to compare against and keeps its text exactly.
    """
    is_new = not path.exists()
    original = "" if is_new else _read_for_preview(path)
    if not is_new:
        original, proposed = _lf(original), _lf(proposed)
    return FileDiff(
        path=path,
        original=original,
        proposed=proposed,
        is_new_file=is_new,
        is_deletion=(proposed == ""),
    )


def diff_stats(diff: FileDiff) -> tuple[int, int]:
    """(added, removed) line counts for a FileDiff."""
    if diff.is_new_file:
        return (len(diff.proposed.splitlines()), 0)
    if diff.is_deletion:
        return (0, len(diff.original.splitlines()))
    added = removed = 0
    for line in difflib.unified_diff(
        diff.original.splitlines(), diff.proposed.splitlines(), lineterm=""
    ):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return (added, removed)


def format_stat_bar(added: int, removed: int, *, width: int = 24, label: str = "") -> str:
    """A `git --stat`-style `+`/`-` bar: a size-at-a-glance summary line.

    Shared by :class:`DiffPreview` and :class:`~velune.execution.hunk_review.
    HunkReviewer` so a very large diff — either a truncated unified diff or a
    long run of individual hunks to click through — shows its scale up front
    instead of making the reviewer infer it by scrolling.
    """
    total = added + removed
    plus_w = round(width * added / total) if total else 0
    minus_w = width - plus_w
    bar = f"[green]{'+' * plus_w}[/green][red]{'-' * minus_w}[/red]"
    prefix = f"{label}  " if label else ""
    return f"  [dim]{prefix}{total} lines changed[/dim]  [green]+{added}[/green] [red]-{removed}[/red]  {bar}"


# ---------------------------------------------------------------------------
# DiffPreview
# ---------------------------------------------------------------------------


class DiffPreview:
    def __init__(self, console) -> None:
        self.console = console

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_diff(self, path: Path, proposed: str) -> FileDiff:
        return compute_file_diff(path, proposed)

    def render_diff(self, diff: FileDiff) -> None:
        from rich.panel import Panel
        from rich.syntax import Syntax

        rel = str(diff.path)

        if diff.is_new_file:
            title = f"[green]NEW FILE[/green] {rel}"
        elif diff.is_deletion:
            title = f"[red]DELETE[/red] {rel}"
        else:
            title = f"[yellow]MODIFY[/yellow] {rel}"

        if diff.is_deletion:
            self.console.print(
                Panel(
                    f"[red]This will delete: {rel}[/red]",
                    title=title,
                    border_style="red",
                )
            )
            return

        if diff.is_new_file:
            lang = self._detect_language(diff.path)
            self.console.print(
                Panel(
                    Syntax(diff.proposed, lang, theme="monokai", line_numbers=True),
                    title=title,
                    border_style="green",
                )
            )
            return

        # Unified diff for modifications
        original_lines = diff.original.splitlines(keepends=True)
        proposed_lines = diff.proposed.splitlines(keepends=True)

        udiff = list(
            difflib.unified_diff(
                original_lines,
                proposed_lines,
                fromfile=f"a/{diff.path.name}",
                tofile=f"b/{diff.path.name}",
                lineterm="",
            )
        )

        if not udiff:
            self.console.print(f"[dim]No changes to {rel}[/dim]")
            return

        is_large = len(udiff) > 200
        diff_text = "\n".join(udiff[:200])
        if is_large:
            diff_text += f"\n... ({len(udiff) - 200} more lines)"
            # Oriented before the wall of (truncated) diff text below — a
            # reviewer deciding accept/reject/skip should know the shape of
            # the change (mostly additions? a near-total rewrite?) before
            # scrolling through hundreds of lines to find out for themselves.
            added, removed = diff_stats(diff)
            self.console.print(format_stat_bar(added, removed))

        self.console.print(
            Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
                title=title,
                border_style="yellow",
                padding=(0, 1),
            )
        )

    async def prompt_decision(
        self,
        diff: FileDiff,
        auto_accept: bool = False,
    ) -> DiffDecision:
        if auto_accept or _auto_accept:
            return DiffDecision.ACCEPT

        from rich.prompt import Prompt

        # No default on purpose: a file mutation must never be accepted by an
        # accidental bare Enter — the user has to type a, r, or s explicitly.
        action = Prompt.ask(
            "\n  [dim][a]ccept / [r]eject / [s]kip all[/dim]",
            choices=["a", "r", "s", "accept", "reject", "skip"],
        )
        mapping = {
            "a": DiffDecision.ACCEPT,
            "accept": DiffDecision.ACCEPT,
            "r": DiffDecision.REJECT,
            "reject": DiffDecision.REJECT,
            "s": DiffDecision.REJECT,
            "skip": DiffDecision.REJECT,
        }
        return mapping.get(action.lower(), DiffDecision.REJECT)

    async def preview_and_confirm(
        self,
        path: Path,
        proposed: str,
        auto_accept: bool = False,
    ) -> DiffDecision:
        diff = self.show(path, proposed)
        return await self.prompt_decision(diff, auto_accept=auto_accept)

    def show(self, path: Path, proposed: str) -> FileDiff:
        """Compute and render the diff for *path*; return it for the decision prompt.

        A file over the editing limit is not loaded to draw a diff: a notice says so, and
        the change is still put to the user for a decision.
        """
        from velune.tools.filesystem.mutation import FileTooLargeError

        try:
            diff = self.compute_diff(path, proposed)
        except FileTooLargeError as exc:
            from rich.markup import escape

            self.console.print(f"[yellow]{escape(str(exc))} No preview is shown.[/yellow]")
            return FileDiff(path, "", proposed, is_new_file=False, is_deletion=(proposed == ""))
        self.render_diff(diff)
        return diff

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _detect_language(self, path: Path) -> str:
        ext_map = {
            ".py": "python",
            ".ts": "typescript",
            ".js": "javascript",
            ".tsx": "tsx",
            ".jsx": "jsx",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".cpp": "cpp",
            ".c": "c",
            ".cs": "csharp",
            ".html": "html",
            ".css": "css",
            ".json": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".toml": "toml",
            ".md": "markdown",
            ".sh": "bash",
        }
        return ext_map.get(path.suffix.lower(), "text")
