"""``edit_file``: replace an exact string in a file.

The targeted alternative to rewriting a whole file with ``write_file``. Matching
is exact — no fuzzy fallback, which could silently change the wrong lines — and
must be unique unless ``replace_all`` is set. The file is read from disk in full
(up to :data:`~velune.tools.filesystem.mutation.MAX_MUTATION_BYTES`), so an edit
never depends on what a possibly truncated ``read_file`` result showed. Writing
goes through :mod:`velune.tools.filesystem.mutation`: atomic, encoding- and
line-ending-preserving, conflict-checked, verified, journaled for ``/rewind``.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem import mutation as mut

_MAX_LISTED_MATCHES = 10


class EditError(ValueError):
    """The edit cannot be applied as asked; the message tells the model how to fix the call."""


def _first_match_lines(text: str, needle: str, limit: int) -> list[int]:
    """Line numbers (1-based) of the first *limit* non-overlapping matches of *needle*.

    Line numbers are counted incrementally from one match to the next, so the cost is one
    pass over the text up to the last match listed — never one pass per match — and nothing
    is kept for matches that will not be shown.
    """
    lines: list[int] = []
    start = 0
    line = 1
    counted_to = 0
    while len(lines) < limit and (index := text.find(needle, start)) != -1:
        line += text.count("\n", counted_to, index)
        counted_to = index
        lines.append(line)
        start = index + len(needle)
    return lines


def _missing_hint(text: str, old_string: str) -> str:
    """Point the model at the closest line when ``old_string`` is not in the file."""
    lines = text.replace("\r\n", "\n").split("\n")
    first = next((ln for ln in old_string.replace("\r\n", "\n").split("\n") if ln.strip()), "")
    squeezed = " ".join(old_string.split())
    if squeezed and squeezed in " ".join(text.split()):
        return (
            " The text is there but its whitespace or indentation differs — copy it exactly "
            "from read_file output (without the line-number prefix)."
        )
    if not first.strip():
        return ""
    stripped = [ln.strip() for ln in lines]
    close = difflib.get_close_matches(first.strip(), stripped, n=1, cutoff=0.6)
    if not close:
        return " Read the file again to see its current content."
    number = stripped.index(close[0]) + 1
    return f" The closest line is {number}: {lines[number - 1].strip()[:120]!r}."


def plan_edit(
    current: mut.TextFile, old_string: str, new_string: str, replace_all: bool = False
) -> tuple[str, int]:
    """``(new text, replacements)`` for an edit of *current*, or raise :class:`EditError`.

    ``old_string`` and ``new_string`` come from the model with LF line endings. In a
    CRLF file a multi-line ``old_string`` is matched in its CRLF form, and the
    replacement is written in the same form, so the file keeps its line endings.
    """
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        raise EditError("old_string and new_string must be strings")
    if old_string == "":
        raise EditError(
            "old_string is empty. To add content, include a nearby existing line in old_string "
            "and repeat it in new_string; to write a new file use write_file."
        )
    if old_string == new_string:
        raise EditError("old_string and new_string are identical; there is nothing to change.")

    text = current.text
    # Try the text as given first, then in the other line-ending form.
    forms = [(old_string, new_string)]
    for newline in ("\r\n", "\n"):
        alt = (mut.to_newline(old_string, newline), mut.to_newline(new_string, newline))
        if alt not in forms:
            forms.append(alt)
    for old, new in forms:
        # ``str.count`` and ``str.replace`` agree on what a match is (non-overlapping, left to
        # right) and run in one linear pass in C, however many matches there are.
        total = text.count(old)
        if not total:
            continue
        if total > 1 and not replace_all:
            # Only a rejected, ambiguous edit needs line numbers, and only for the few that
            # the message lists.
            listed = _first_match_lines(text, old, _MAX_LISTED_MATCHES)
            shown = ", ".join(str(n) for n in listed)
            more = "…" if total > _MAX_LISTED_MATCHES else ""
            raise EditError(
                f"old_string matches {total} places (lines {shown}{more}). Include more "
                "surrounding lines so it matches exactly one place, or set replace_all=true to "
                "change every occurrence."
            )
        count = total if replace_all else 1
        return text.replace(old, new, -1 if replace_all else 1), count
    raise EditError(f"old_string was not found in the file.{_missing_hint(text, old_string)}")


class EditFile(BaseTool):
    """Replace an exact string in an existing file."""

    def __init__(
        self, workspace: Path | None = None, tracker: mut.FileVersionTracker | None = None
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.tracker = tracker or mut.FileVersionTracker()

    def get_name(self) -> str:
        return "edit_file"

    def get_description(self) -> str:
        return (
            "Change part of an existing file by replacing old_string with new_string. "
            "old_string must match the file exactly, including indentation and whitespace "
            "(copy it from read_file output without the line-number prefix), and must match "
            "exactly one place unless replace_all=true. Prefer this to write_file for changes "
            "to existing files. Returns a diff of the change."
        )

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [
            path_action(ActionType.MODIFY_FILE, args.get("file_path", ""), boundary, "edit file")
        ]

    def preview(self, args: dict[str, Any]) -> tuple[Path, str] | None:
        """``(path, proposed full text)`` for the approval diff, or None if the edit is invalid."""
        try:
            path = resolve_tool_path(args.get("file_path", ""), self.workspace, label="edit_file")
            current = mut.load_text(path)
            new_text, _ = plan_edit(
                current,
                args.get("old_string", ""),
                args.get("new_string", ""),
                bool(args.get("replace_all", False)),
            )
            return path, new_text
        except Exception:
            return None

    async def execute(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        expected_sha256: str | None = None,
    ) -> str:
        path = resolve_tool_path(file_path, self.workspace, label="edit_file")
        label = mut.display_path(path, self.workspace)
        current = mut.load_text(path, label)
        if expected_sha256 and expected_sha256 != current.sha256:
            raise mut.FileConflictError(mut.conflict_message(path, expected_sha256, current.sha256))
        self.tracker.check(path, current.sha256)

        try:
            new_text, count = plan_edit(current, old_string, new_string, bool(replace_all))
        except EditError as exc:
            raise EditError(f"{label}: {exc}") from None

        data = mut.encode_text(new_text, current.encoding, current.bom)
        new_sha = mut.atomic_write(path, data, expected_sha256=current.sha256)
        mut.verify_written(path, new_sha)
        self.tracker.record(path, new_sha)
        mut.journal(mut.FileChange(path, current.raw, new_sha))

        diff, added, removed = mut.unified_diff(current.text, new_text, label)
        notes = [f"{count} replacements"] if count > 1 else []
        return mut.MutationResult(
            "edit_file", label, added, removed, diff, True, new_sha, notes
        ).render()

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to edit (relative to the workspace)",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace (must match the file exactly)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Text to put in its place (may be empty to delete it)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring exactly one",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": (
                        "Optional. The sha256 from your last read/write of this file; the edit "
                        "is refused if the file has changed since."
                    ),
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }
