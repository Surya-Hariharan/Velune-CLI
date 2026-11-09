"""Filesystem write tools: write, create and delete files.

Every change goes through :mod:`velune.tools.filesystem.mutation`: atomic
replacement, preserved encoding and line endings, conflict detection against
the version the agent last saw, verification after the write, a unified diff
in the result, and a record in the turn's undo journal (``/rewind``).

With ``confirm=True`` (the default, legacy behavior) a write first renders a
diff preview to a Rich console and blocks on user approval. The interactive
REPL builds these tools with ``confirm=False``: approval happens once in the
tool loop's permission gate (which shows the diff), so the tool itself must
neither print nor prompt — its default ``Console()`` would write to raw stdout
and its ``Prompt.ask`` would collide with the fullscreen prompt_toolkit app.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem import mutation as mut

if TYPE_CHECKING:
    from rich.console import Console


class _WriteToolBase(BaseTool):
    """Shared constructor/console plumbing for the filesystem write tools."""

    def __init__(
        self,
        workspace: Path | None = None,
        console: Console | None = None,
        confirm: bool = True,
        tracker: mut.FileVersionTracker | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.confirm = confirm
        self._console = console
        self.tracker = tracker or mut.FileVersionTracker()

    @property
    def console(self) -> Console:
        # Built lazily so the confirm=False path never constructs a detached
        # stdout console at all.
        if self._console is None:
            from rich.console import Console

            self._console = Console()
        return self._console

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def _display(self, path: Path) -> str:
        return mut.display_path(path, self.workspace)


_EXPECTED_SHA_SCHEMA = {
    "type": "string",
    "description": (
        "Optional. The sha256 reported by your last read_file/write_file/edit_file of this "
        "file; the change is refused if the file has changed since."
    ),
}


class WriteFile(_WriteToolBase):
    """Write a whole file (create it, or replace all of its content)."""

    def get_name(self) -> str:
        return "write_file"

    def get_description(self) -> str:
        return (
            "Write the complete content of a file, creating it (and missing folders) if needed. "
            "Replaces everything in an existing file — to change part of a file use edit_file. "
            "An existing file keeps its encoding and line endings. Returns a diff of the change."
        )

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        target = boundary.resolve(args.get("file_path", ""))
        kind = ActionType.MODIFY_FILE if target.exists() else ActionType.CREATE_FILE
        return [path_action(kind, target, boundary, "write file contents")]

    def prepare(self, path: Path, content: str) -> tuple[bytes, str, str, bytes | None]:
        """``(new bytes, old text, expected hash, previous bytes)`` for writing *content* to *path*.

        An existing text file keeps its encoding, BOM and dominant line ending; a new file
        (or a binary file being replaced) is written as UTF-8 exactly as given.
        """
        if path.is_dir():
            raise IsADirectoryError(f"{self._display(path)} is a folder, not a file")
        if not path.exists():
            return content.encode("utf-8"), "", mut.ABSENT, None
        try:
            current = mut.load_text(path, self._display(path))
        except ValueError:
            # Binary or undecodable: replaced wholesale as UTF-8.
            raw = path.read_bytes()
            return content.encode("utf-8"), "", mut.sha256_bytes(raw), raw
        text = mut.to_newline(content, current.newline) if current.style != "none" else content
        data = mut.encode_text(text, current.encoding, current.bom)
        return data, current.text, current.sha256, current.raw

    async def execute(
        self, file_path: str, content: str, expected_sha256: str | None = None
    ) -> str:
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        path = resolve_tool_path(file_path, self.workspace, label="WriteFile")
        data, old_text, current_sha, before = self.prepare(path, content)
        if expected_sha256 and expected_sha256 != current_sha:
            raise mut.FileConflictError(mut.conflict_message(path, expected_sha256, current_sha))
        self.tracker.check(path, current_sha)

        if self.confirm:
            from velune.execution.diff_preview import DiffDecision, DiffPreview

            decision = await DiffPreview(self.console).preview_and_confirm(path, content)
            if decision != DiffDecision.ACCEPT:
                self.console.print(f"[yellow]Skipped:[/yellow] {path}")
                return f"Skipped (rejected by user): {path}"

        new_sha = mut.atomic_write(path, data, expected_sha256=current_sha)
        mut.verify_written(path, new_sha)
        self.tracker.record(path, new_sha)
        mut.journal(mut.FileChange(path, before, new_sha))

        label = self._display(path)
        diff, added, removed = mut.unified_diff(old_text, content, label)
        notes = [] if before is not None else ["(new file)"]
        if self.confirm:
            self.console.print(f"[green]Written:[/green] {path}")
        return mut.MutationResult(
            "write_file", label, added, removed, diff, True, new_sha, notes
        ).render()

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to write (relative to the workspace)",
                },
                "content": {
                    "type": "string",
                    "description": "The complete new content of the file",
                },
                "expected_sha256": _EXPECTED_SHA_SCHEMA,
            },
            "required": ["file_path", "content"],
        }


class CreateFile(_WriteToolBase):
    """Create a new, empty file."""

    def get_name(self) -> str:
        return "create_file"

    def get_description(self) -> str:
        return (
            "Create a new empty file (and any missing folders). Fails if the file already "
            "exists. To create a file with content, use write_file."
        )

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [
            path_action(ActionType.CREATE_FILE, args.get("file_path", ""), boundary, "create file")
        ]

    async def execute(self, file_path: str) -> str:
        path = resolve_tool_path(file_path, self.workspace, label="CreateFile")
        label = self._display(path)
        if path.exists():
            kind = "folder" if path.is_dir() else "file"
            raise FileExistsError(
                f"{label} already exists (a {kind}). Use write_file or edit_file to change a file."
            )

        if self.confirm:
            from velune.execution.diff_preview import DiffDecision, DiffPreview

            decision = await DiffPreview(self.console).preview_and_confirm(path, "")
            if decision != DiffDecision.ACCEPT:
                self.console.print(f"[yellow]Skipped:[/yellow] {path}")
                return f"Skipped (rejected by user): {path}"

        new_sha = mut.atomic_write(path, b"", expected_sha256=mut.ABSENT)
        mut.verify_written(path, new_sha)
        self.tracker.record(path, new_sha)
        mut.journal(mut.FileChange(path, None, new_sha))
        if self.confirm:
            self.console.print(f"[green]Created:[/green] {path}")
        return mut.MutationResult("create_file", label, 0, 0, "", True, new_sha).render()

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path of the new file (relative to the workspace)",
                },
            },
            "required": ["file_path"],
        }


class DeleteFile(_WriteToolBase):
    """Delete a file."""

    def get_name(self) -> str:
        return "delete_file"

    def get_description(self) -> str:
        return "Delete a file. To delete a folder use delete_directory."

    def describe_actions(self, args, boundary):
        from velune.permissions.actions import ActionType, Risk
        from velune.permissions.boundary import path_action

        return [
            path_action(
                ActionType.DELETE_FILE,
                args.get("file_path", ""),
                boundary,
                "delete file",
                risk=Risk.MEDIUM,
            )
        ]

    async def execute(self, file_path: str) -> str:
        path = resolve_tool_path(file_path, self.workspace, label="DeleteFile")
        label = self._display(path)
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(f"File not found: {file_path}")
        if path.is_dir():
            raise IsADirectoryError(f"{label} is a folder; use delete_directory")

        if self.confirm:
            from velune.execution.diff_preview import DiffDecision, DiffPreview

            # proposed="" marks this as a deletion in FileDiff
            decision = await DiffPreview(self.console).preview_and_confirm(path, "")
            if decision != DiffDecision.ACCEPT:
                self.console.print(f"[yellow]Skipped:[/yellow] {path}")
                return f"Skipped (rejected by user): {path}"

        if path.is_symlink():
            before, kept = None, False  # a link is not restored as a file
        else:
            before, kept = mut.read_before_image(path)
        removed = len(before.decode("utf-8", errors="replace").splitlines()) if before else 0
        path.unlink()
        if path.exists() or path.is_symlink():
            raise mut.MutationVerificationError(f"{label} still exists after deleting it")
        self.tracker.forget(path)
        if kept:
            mut.journal(mut.FileChange(path, before if before is not None else b"", mut.ABSENT))
        else:
            mut.journal_irreversible(f"deleted {path}")
        if self.confirm:
            self.console.print(f"[red]Deleted:[/red] {path}")
        notes = [] if kept else ["(too large to keep a copy: /rewind cannot restore it)"]
        return mut.MutationResult("delete_file", label, 0, removed, "", True, None, notes).render()

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to delete (relative to the workspace)",
                },
            },
            "required": ["file_path"],
        }
