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


def compute_file_diff(path: Path, proposed: str) -> FileDiff:
    """Build a FileDiff against the current on-disk state (no console needed)."""
    is_new = not path.exists()
    original = "" if is_new else path.read_text(errors="replace")
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

        diff_text = "\n".join(udiff[:200])
        if len(udiff) > 200:
            diff_text += f"\n... ({len(udiff) - 200} more lines)"

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

        action = Prompt.ask(
            "\n  [dim][a]ccept / [r]eject / [s]kip all[/dim]",
            choices=["a", "r", "s", "accept", "reject", "skip"],
            default="a",
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
        diff = self.compute_diff(path, proposed)
        self.render_diff(diff)
        return await self.prompt_decision(diff, auto_accept=auto_accept)

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
