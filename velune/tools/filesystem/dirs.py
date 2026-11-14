"""Folder and move/rename tools.

Implemented in Python rather than by shelling out, so they work the same on
Windows (where ``mkdir``/``rmdir``/``move`` are cmd.exe builtins the
shell-less sandbox can't run) and on POSIX. Every call is described as
actions and authorized by the permission gate before it runs; paths resolve
against the session boundary (the workspace, plus any location the user
granted for this task). Each change is verified after it is made and recorded
in the turn's undo journal (``/rewind``), see
:mod:`velune.tools.filesystem.mutation`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from velune.permissions.gate import resolve_tool_path
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.filesystem import mutation as mut


class _FsTool(BaseTool):
    def __init__(
        self, workspace: Path | None = None, tracker: mut.FileVersionTracker | None = None
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.tracker = tracker or mut.FileVersionTracker()

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def _display(self, path: Path) -> str:
        return mut.display_path(path, self.workspace)


class CreateDirectory(_FsTool):
    def get_name(self) -> str:
        return "create_directory"

    def get_description(self) -> str:
        return "Create a folder (and any missing parent folders)."

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Folder to create"}},
            "required": ["path"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType
        from velune.permissions.boundary import path_action

        return [
            path_action(
                ActionType.CREATE_DIRECTORY, args.get("path", ""), boundary, "create folder"
            )
        ]

    async def execute(self, path: str) -> str:
        target = resolve_tool_path(path, self.workspace, label="create_directory")
        if target.exists() and not target.is_dir():
            raise FileExistsError(f"A file already exists at {target}")
        if target.is_dir():
            return f"Folder already existed: {target}"
        # Record every folder this call creates, outermost first, so /rewind removes them all.
        created: list[Path] = []
        probe = target
        while not probe.exists():
            created.append(probe)
            probe = probe.parent
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise mut.MutationVerificationError(f"{target} is not a folder after creating it")
        for folder in reversed(created):
            mut.journal(mut.DirCreated(folder))
        return f"Created folder {target}"


class DeleteDirectory(_FsTool):
    def get_name(self) -> str:
        return "delete_directory"

    def get_description(self) -> str:
        return (
            "Delete a folder. Set recursive=true to delete a folder that still has "
            "contents (high-risk: always asks for confirmation)."
        )

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to delete"},
                "recursive": {
                    "type": "boolean",
                    "description": "Also delete everything inside the folder (default false)",
                },
            },
            "required": ["path"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType, Risk
        from velune.permissions.boundary import path_action

        target = boundary.resolve(args.get("path", ""))
        non_empty = target.is_dir() and any(target.iterdir())
        recursive = bool(args.get("recursive")) and non_empty
        is_root = target == boundary.workspace_root
        risk = Risk.HIGH if (recursive or is_root) else Risk.MEDIUM
        reason = (
            "delete the whole workspace"
            if is_root
            else ("recursive deletion" if recursive else "delete empty folder")
        )
        # The path as requested, not the resolved one: protection judges both identities.
        return [
            path_action(
                ActionType.DELETE_DIRECTORY, args.get("path", ""), boundary, reason, risk=risk
            )
        ]

    async def execute(self, path: str, recursive: bool = False) -> str:
        target = resolve_tool_path(path, self.workspace, label="delete_directory")
        if not target.exists():
            raise FileNotFoundError(f"Folder not found: {target}")
        if not target.is_dir() or mut.is_link(target):
            raise NotADirectoryError(f"Not a folder: {target} (use delete_file)")
        if any(target.iterdir()):
            if not recursive:
                raise OSError(
                    f"Folder is not empty: {target}. Pass recursive=true to delete its contents."
                )
            count = mut.count_entries(target)
            tree = mut.capture_tree(target)
            shutil.rmtree(target)
            message = f"Deleted folder {target} and {count} item(s) inside it"
        else:
            tree = mut.TreeSnapshot([], {})
            target.rmdir()
            message = f"Deleted empty folder {target}"
        if target.exists():
            raise mut.MutationVerificationError(f"{target} still exists after deleting it")
        snapshot = None if tree is None else tree.entries
        mut.journal(mut.DirDeleted(target, snapshot, None if tree is None else tree.modes))
        if snapshot is None:
            message += " (too large to keep a copy: /rewind cannot restore it)"
        return message


class MovePath(_FsTool):
    def get_name(self) -> str:
        return "move_path"

    def get_description(self) -> str:
        return "Move or rename a file or folder."

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Existing file or folder"},
                "destination": {"type": "string", "description": "New path"},
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing destination file (default false)",
                },
            },
            "required": ["source", "destination"],
        }

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import ActionType, Risk
        from velune.permissions.boundary import path_action, protected_descendant_reason

        raw_source, raw_dest = args.get("source", ""), args.get("destination", "")
        source = boundary.resolve(raw_source)
        dest = boundary.resolve(raw_dest)
        overwrite = bool(args.get("overwrite")) and dest.exists()
        risk = Risk.MEDIUM if overwrite else Risk.LOW
        # A folder carries its contents with it: moving one onto ``.velune`` (or a folder that
        # holds ``.git``/``.velune``/``velune.toml``) installs protected files at the destination
        # without ever naming them. Judge what the move would put there.
        installs = (
            protected_descendant_reason(source, dest, boundary.protected_roots())
            if source.is_dir()
            else ""
        )
        dest_reason = "move destination"
        dest_risk = risk
        if installs:
            dest_risk = Risk.HIGH
            dest_reason = f"move destination: {installs}"
        # Resolving rewrites an existing name to its stored spelling on Windows; show a
        # case-only rename with the spelling asked for, not "Readme.md → Readme.md".
        requested = Path(str(raw_dest)).name
        shown = dest
        if (
            requested
            and requested != dest.name
            and mut.fold_name(requested) == mut.fold_name(dest.name)
        ):
            shown = dest.parent / requested
        # The paths are passed as requested (not resolved) so a link's own name is judged too.
        return [
            path_action(
                ActionType.MOVE, raw_source, boundary, "move / rename", risk=risk, detail=str(shown)
            ),
            # The destination is checked too: moving *into* an outside or secret
            # location must be caught just like moving out of one.
            path_action(ActionType.CREATE_FILE, raw_dest, boundary, dest_reason, risk=dest_risk),
        ]

    async def execute(self, source: str, destination: str, overwrite: bool = False) -> str:
        src = resolve_tool_path(source, self.workspace, label="move_path source")
        dst = resolve_tool_path(destination, self.workspace, label="move_path destination")
        if not src.exists():
            raise FileNotFoundError(f"Not found: {src}")
        respelled = _case_only_rename(src, dst, destination, self.workspace)
        if respelled is not None:
            return self._rename_case_only(*respelled)
        if src == dst:
            raise ValueError(f"Source and destination are the same: {src}")
        if src.is_dir() and (dst == src or src in dst.parents):
            raise ValueError(f"Cannot move folder {src} into itself ({dst})")
        if dst.exists() and not overwrite:
            raise FileExistsError(
                f"Destination already exists: {dst}. Pass overwrite=true to replace a file."
            )
        replacing = dst.exists()
        if replacing and (dst.is_dir() or src.is_dir()):
            raise FileExistsError(f"Cannot overwrite {dst}: only a file can replace a file")

        # Conflict checks, all before anything is moved (#34). A file the agent read and that
        # changed since is never moved, and never overwritten, without it seeing the change.
        is_file = src.is_file()
        sha_before = mut.file_sha256(src) if is_file else None
        if sha_before is not None:
            self.tracker.check(src, sha_before)
        overwritten: bytes | None = None
        overwritten_mode: int | None = None
        kept = True
        dst_sha: str | None = None
        if replacing:
            overwritten, kept = mut.read_before_image(dst)
            # Hash the bytes kept for /rewind, so the version checked is the one kept.
            dst_sha = (
                mut.sha256_bytes(overwritten)
                if overwritten is not None and kept
                else mut.file_sha256(dst)
            )
            self.tracker.check(dst, dst_sha)
            overwritten_mode = mut.file_mode(dst)

        mut.ensure_parent(dst)
        if is_file and sha_before is not None:
            # Both are checked again right before the swap: the before-image read above takes
            # time, and a change made meanwhile must not be moved or overwritten unseen. The
            # source first: a source change slipping in during the destination check is still
            # moved intact and fails the content check after the move. The destination last,
            # nearest the swap, as atomic_write does: an edit there that slipped through would
            # be overwritten, and that cannot be undone.
            current = mut.file_sha256(src)
            if current != sha_before:
                raise mut.FileConflictError(mut.conflict_message(src, sha_before, current))
            if dst_sha is not None:
                current = mut.file_sha256(dst)
                if current != dst_sha:
                    raise mut.FileConflictError(mut.conflict_message(dst, dst_sha, current))
            try:
                # Atomic on one filesystem, including replacing an existing destination file.
                os.replace(src, dst)
            except OSError:
                if dst.exists():
                    raise
                _copy_across_filesystems(src, dst, sha_before)
        else:
            shutil.move(str(src), str(dst))

        # Journaled as soon as the move has happened, before the checks below can raise: a
        # replaced destination must stay recoverable even if verification then fails. A
        # rewind re-checks the moved file's content, so an entry for a move that went wrong
        # is skipped there rather than acted on blindly.
        if replacing and not kept:
            mut.journal_irreversible(f"overwrote {dst}")
        mut.journal(mut.Moved(src, dst, overwritten, sha_before, overwritten_mode))

        if not dst.exists() or src.exists():
            raise mut.MutationVerificationError(f"Move of {src} to {dst} did not complete")
        if sha_before is not None and mut.file_sha256(dst) != sha_before:
            raise mut.MutationVerificationError(f"{dst} does not have {src}'s content after moving")

        if is_file:
            self.tracker.forget(src)
            if sha_before is not None:
                self.tracker.record(dst, sha_before)
        return f"Moved {src} → {dst} (verified)"

    def _rename_case_only(self, src: Path, dst: Path) -> str:
        """Respell *src* as *dst* (same entry, a case-insensitive filesystem) (#37)."""
        is_file = src.is_file()
        sha_before = mut.file_sha256(src) if is_file else None
        if sha_before is not None:
            self.tracker.check(src, sha_before)
        renamed = mut.respell(src, dst)
        mut.journal(mut.Moved(src, renamed, None, sha_before))  # before checks that can raise
        listing = os.listdir(renamed.parent)
        if renamed.name not in listing or src.name in listing:
            raise mut.MutationVerificationError(f"Renaming {src} to {renamed} did not complete")
        if sha_before is not None and mut.file_sha256(renamed) != sha_before:
            raise mut.MutationVerificationError(f"{renamed} does not have {src}'s content")
        if is_file:
            self.tracker.forget(src)
            if sha_before is not None:
                self.tracker.record(renamed, sha_before)
        return f"Renamed {src} → {renamed} (case only, verified)"


def _copy_across_filesystems(src: Path, dst: Path, sha: str) -> None:
    """Move a file to another filesystem: copy, check both sides, then remove the source.

    ``os.replace`` cannot cross filesystems, so this is not atomic: for a moment both copies
    exist. The copy is created exclusively (a file that appeared at *dst* is never
    replaced), and the source is removed only if both the copy and the source still hash to
    the version that was checked. A source changed during the copy is left in place and the
    copy is removed, so nothing the agent did not see is ever deleted. A change in the short
    gap between that last check and the removal is not caught: no filesystem API offers
    "delete only if unchanged".

    The copy is created owner-only (0600) and given the source's permissions only once its
    content is complete, so a private file is never readable by others, not even while it
    is being copied. If the copy fails, the partial file (ours: it was created exclusively)
    is removed.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    writer = os.fdopen(os.open(dst, flags, 0o600), "wb")  # owns the descriptor from here
    try:
        with writer, open(src, "rb") as reader:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise
    try:
        shutil.copystat(src, dst)  # the source's permissions and times, now the content is in
    except OSError:
        pass  # best effort; the content is what matters, and the copy stays owner-only
    copied, current = mut.file_sha256(dst), mut.file_sha256(src)
    if current != sha:
        os.unlink(dst)
        raise mut.FileConflictError(mut.conflict_message(src, sha, current))
    if copied != sha:
        os.unlink(dst)
        raise mut.MutationVerificationError(f"The copy of {src} at {dst} does not match it")
    os.unlink(src)


def _case_only_rename(
    src: Path, dst: Path, raw_destination: str, workspace: Path
) -> tuple[Path, Path] | None:
    """``(source as spelled on disk, destination as requested)`` for a case-only rename.

    Resolving a path on Windows rewrites an existing name to its stored spelling, so the
    spelling asked for is taken from the destination argument itself. Anything other than a
    respelling of the same entry in the same folder returns None and is moved as usual.
    The destination as typed must itself be that folder's entry, not a link that resolves
    to it: a link's name says nothing about how its target should be spelled.
    """
    raw = Path(raw_destination).expanduser()
    lexical = Path(os.path.abspath(raw if raw.is_absolute() else workspace / raw))
    if not lexical.name or mut.fold_name(lexical.name) != mut.fold_name(src.name):
        return None  # not two spellings of one name: an ordinary move (the common case)
    if os.path.islink(lexical):
        return None
    current = mut.on_disk_spelling(src)
    wanted = dst.parent / lexical.name
    if not os.path.isdir(lexical.parent) or not os.path.samefile(lexical.parent, current.parent):
        return None
    if not mut.is_respelling(current, wanted):
        return None
    return current, wanted
