"""Whole-file edit format parser.

The LLM outputs entire file contents wrapped in a fenced code block preceded
by the file path:

    path/to/file.py
    ```python
    # complete file contents here
    ```

Deletions are expressed as:

    DELETE: path/to/file.py

Multiple files may appear in a single response.
"""

from __future__ import annotations

import re
from pathlib import Path

from velune.execution.edit_formats.base import BaseEditFormat, EditBlock, EditFormat, ParseError

# Primary pattern: path on its own line, then fenced block immediately after.
_FILE_BLOCK_RE = re.compile(
    r"(?:^|\n)"
    r"(?:(?:File|file|###|##)\s*:?\s*)?"  # optional "File:" or heading prefix
    r"([\w./\\-]+\.[a-zA-Z0-9]+)"  # file path
    r"[ \t]*\n"
    r"```([a-zA-Z0-9]*)\n"  # opening fence + optional lang
    r"(.*?)"  # file contents
    r"```",
    re.DOTALL,
)

# Deletion marker pattern.
_DELETE_RE = re.compile(
    r"(?:^|\n)DELETE\s*:\s*([\w./\\-]+\.[a-zA-Z0-9]+)",
    re.IGNORECASE,
)


class WholeFileFormat(BaseEditFormat):
    """Parser for the path + fenced-block whole-file output format."""

    def parse(self, response: str, workspace_path: Path | None = None) -> list[EditBlock]:
        blocks: list[EditBlock] = []

        # Whole-file rewrites
        for m in _FILE_BLOCK_RE.finditer(response):
            file_path = m.group(1).strip()
            content = m.group(3)

            if not file_path or len(file_path) > 260:
                continue

            is_new = workspace_path is None or not (workspace_path / file_path).exists()

            original = ""
            if not is_new and workspace_path:
                target = workspace_path / file_path
                if target.exists():
                    original = target.read_text(encoding="utf-8", errors="replace")
                    is_new = False

            blocks.append(
                EditBlock(
                    file_path=file_path,
                    original=original,
                    proposed=content,
                    is_new_file=is_new,
                    is_deletion=False,
                    format_used=EditFormat.WHOLE_FILE,
                )
            )

        # Explicit deletions
        for m in _DELETE_RE.finditer(response):
            file_path = m.group(1).strip()
            if file_path:
                blocks.append(
                    EditBlock(
                        file_path=file_path,
                        original="",
                        proposed="",
                        is_new_file=False,
                        is_deletion=True,
                        format_used=EditFormat.WHOLE_FILE,
                    )
                )

        if not blocks:
            raise ParseError("No whole-file blocks found in response")
        return blocks

    def format_instructions(self) -> str:
        return """\
### [EDIT FORMAT: WHOLE FILE]
Output each modified or new file in full using this structure:

path/to/file.py
```python
# complete updated file contents — never truncate
```

Rules:
- Write the ENTIRE file, not just the changed sections.
- Place the file path on its own line immediately before the opening fence.
- Use the correct language identifier in the fence (python, typescript, go, etc.).
- To delete a file, write: DELETE: path/to/file.py
- You may output multiple files in a single response.
"""
