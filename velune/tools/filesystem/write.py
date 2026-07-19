"""Filesystem write tools.

With ``confirm=True`` (the default, legacy behavior) every write renders a
diff preview to a Rich console and blocks on user approval. The interactive
REPL builds these tools with ``confirm=False``: approval happens once in the
tool loop's approver (which shows the diff), so the tool itself must neither
print nor prompt — its default ``Console()`` would write to raw stdout and
its ``Prompt.ask`` would collide with the fullscreen prompt_toolkit app.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from velune.execution.path_guard import resolve_in_workspace
from velune.tools.base.tool import BaseTool, ToolPermission

if TYPE_CHECKING:
    from rich.console import Console


class _WriteToolBase(BaseTool):
    """Shared constructor/console plumbing for the filesystem write tools."""

    def __init__(
        self,
        workspace: Path | None = None,
        console: Console | None = None,
        confirm: bool = True,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.confirm = confirm
        self._console = console

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


class WriteFile(_WriteToolBase):
    """Write content to a file, showing a diff preview first."""

    def get_name(self) -> str:
        return "write_file"

    def get_description(self) -> str:
        return "Write content to a file (shows diff preview before writing)"

    async def execute(self, file_path: str, content: str) -> str:
        path = resolve_in_workspace(file_path, self.workspace, label="WriteFile")
        if not self.confirm:
            from velune.execution.diff_preview import compute_file_diff, diff_stats

            added, removed = diff_stats(compute_file_diff(path, content))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            total = len(content.splitlines())
            return f"Wrote {total} lines to {path} (+{added} -{removed})"
        return await self._write_file_with_preview(path, content)

    async def _write_file_with_preview(
        self,
        path: Path,
        content: str,
        auto_accept: bool = False,
    ) -> str:
        from velune.execution.diff_preview import DiffDecision, DiffPreview

        preview = DiffPreview(self.console)
        decision = await preview.preview_and_confirm(path, content, auto_accept=auto_accept)
        if decision == DiffDecision.ACCEPT:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.console.print(f"[green]Written:[/green] {path}")
            return f"Successfully wrote to {path}"
        self.console.print(f"[yellow]Skipped:[/yellow] {path}")
        return f"Skipped (rejected by user): {path}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        }


class CreateFile(_WriteToolBase):
    """Create an empty file, showing a diff preview first."""

    def get_name(self) -> str:
        return "create_file"

    def get_description(self) -> str:
        return "Create an empty file (shows preview before creating)"

    async def execute(self, file_path: str) -> str:
        path = resolve_in_workspace(file_path, self.workspace, label="CreateFile")

        if not self.confirm:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            return f"Created {file_path}"

        from velune.execution.diff_preview import DiffDecision, DiffPreview

        preview = DiffPreview(self.console)
        # Treat create-empty as a write of empty content so the diff shows "NEW FILE"
        decision = await preview.preview_and_confirm(path, "", auto_accept=False)
        if decision == DiffDecision.ACCEPT:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            self.console.print(f"[green]Created:[/green] {path}")
            return f"Successfully created {file_path}"
        self.console.print(f"[yellow]Skipped:[/yellow] {path}")
        return f"Skipped (rejected by user): {path}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to create",
                },
            },
            "required": ["file_path"],
        }


class DeleteFile(_WriteToolBase):
    """Delete a file, showing a deletion preview first."""

    def get_name(self) -> str:
        return "delete_file"

    def get_description(self) -> str:
        return "Delete a file (shows preview before deleting)"

    async def execute(self, file_path: str) -> str:
        path = resolve_in_workspace(file_path, self.workspace, label="DeleteFile")
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if not self.confirm:
            removed = len(path.read_text(errors="replace").splitlines())
            path.unlink()
            return f"Deleted {file_path} ({removed} lines)"

        from velune.execution.diff_preview import DiffDecision, DiffPreview

        # proposed="" marks this as a deletion in FileDiff
        preview = DiffPreview(self.console)
        decision = await preview.preview_and_confirm(path, "", auto_accept=False)
        if decision == DiffDecision.ACCEPT:
            path.unlink()
            self.console.print(f"[red]Deleted:[/red] {path}")
            return f"Successfully deleted {file_path}"
        self.console.print(f"[yellow]Skipped:[/yellow] {path}")
        return f"Skipped (rejected by user): {path}"

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to delete",
                },
            },
            "required": ["file_path"],
        }
