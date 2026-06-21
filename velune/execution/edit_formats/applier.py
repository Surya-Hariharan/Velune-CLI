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
        """Write *content* to *target*, creating parent directories if needed."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if content == "":
            if target.exists():
                target.unlink()
                logger.info("Deleted: %s", target)
        else:
            target.write_text(content, encoding="utf-8")
            logger.info("Written: %s", target)
