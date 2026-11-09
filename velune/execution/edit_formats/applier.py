"""EditBlock applier — resolves each block to a final file content string.

The applier reads the current on-disk state and computes what the file should
look like after the edit.  It does NOT write anything to disk; the caller
(REPL or test) controls the write after the user has confirmed the diff.
"""

from __future__ import annotations

import logging
from pathlib import Path

from velune.execution.edit_formats.base import EditBlock, EditFormat, ParseError
from velune.execution.edit_formats.search_replace import SearchReplaceFormat

logger = logging.getLogger("velune.execution.edit_formats.applier")

_sr_parser = SearchReplaceFormat()


class EditBlockApplier:
    """Resolves EditBlock objects to (path, new_content) pairs ready for writing."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path).resolve()

    def resolve(self, block: EditBlock) -> tuple[Path, str]:
        """Return *(absolute_path, new_content)* for one EditBlock.

        Raises ParseError if a SEARCH/REPLACE block cannot be matched.
        Raises ValueError for unsupported combinations.
        """
        target = self.workspace_path / block.file_path

        if block.is_deletion:
            return target, ""

        if block.format_used == EditFormat.SEARCH_REPLACE:
            content = _sr_parser.apply_block(block, self.workspace_path)
            return target, content

        if block.format_used in (EditFormat.WHOLE_FILE, EditFormat.UDIFF):
            return target, block.proposed

        raise ValueError(f"Unsupported edit format: {block.format_used}")

    def resolve_all(self, blocks: list[EditBlock]) -> list[tuple[Path, str]]:
        """Resolve a list of blocks, skipping any that fail with a warning."""
        results: list[tuple[Path, str]] = []
        for block in blocks:
            try:
                results.append(self.resolve(block))
            except ParseError as exc:
                logger.warning("Skipping edit block for %s: %s", block.file_path, exc)
        return results

    def write(self, target: Path, content: str) -> None:
        """Write *content* to *target* (empty content deletes it).

        Goes through the same safe-mutation layer as the agent's file tools
        (velune/tools/filesystem/mutation.py): the target must stay inside the
        workspace, an existing file keeps its encoding and line endings, the swap is
        atomic, the result is verified, and the change is journaled for /rewind.
        """
        from velune.execution.path_guard import resolve_in_workspace
        from velune.tools.filesystem import mutation as mut

        target = resolve_in_workspace(target, self.workspace_path, label="council edit")
        if content == "":
            if target.exists():
                before, kept = mut.read_before_image(target)
                target.unlink()
                if kept:
                    mut.journal(mut.FileChange(target, before or b"", mut.ABSENT))
                else:
                    mut.journal_irreversible(f"deleted {target}")
                logger.info("Deleted: %s", target)
            return

        if target.exists():
            try:
                current = mut.load_text(target)
            except ValueError:  # binary: replaced wholesale
                current = None
        else:
            current = None
        if current is not None:
            text = mut.to_newline(content, current.newline) if current.style != "none" else content
            data = mut.encode_text(text, current.encoding, current.bom)
            before, expected = current.raw, current.sha256
        else:
            data = content.encode("utf-8")
            before, _kept = mut.read_before_image(target)
            expected = mut.file_sha256(target)
        new_sha = mut.atomic_write(target, data, expected_sha256=expected)
        mut.verify_written(target, new_sha)
        mut.journal(mut.FileChange(target, before, new_sha))
        logger.info("Written: %s", target)
