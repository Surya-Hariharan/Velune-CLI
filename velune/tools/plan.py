"""``write_plan``: the one tool that may write while planning.

It can only write Markdown plans under ``<workspace>/.velune/plans/`` — the
permission policy enforces that (``WRITE_PLAN`` actions targeting anywhere
else are denied), and the tool itself only ever builds a path there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from velune.permissions.plans import WAITING, parse_status, set_status, slugify
from velune.tools.base.tool import BaseTool, ToolPermission


class WritePlan(BaseTool):
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or Path.cwd())

    def get_name(self) -> str:
        return "write_plan"

    def get_description(self) -> str:
        return (
            "Write or update the execution plan (Markdown) in .velune/plans/<name>.md. "
            "In plan mode this is the only change you may make. Use the same name to "
            "revise a plan. Include a 'Files Affected' table (| Action | File |) and end "
            "with '## Approval' / 'Status: WAITING_FOR_USER_APPROVAL'."
        )

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.FILESYSTEM_WRITE}

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short plan name, e.g. 'fix-auth'"},
                "content": {"type": "string", "description": "The full plan in Markdown"},
            },
            "required": ["name", "content"],
        }

    def _path(self, name: str) -> Path:
        return (self.workspace / ".velune" / "plans" / f"{slugify(name)}.md").resolve()

    def describe_actions(self, args: dict[str, Any], boundary: Any) -> list[Any]:
        from velune.permissions.actions import Action, ActionType

        target = self._path(str(args.get("name", "plan")))
        return [Action(ActionType.WRITE_PLAN, str(target), "write the execution plan")]

    async def execute(self, name: str, content: str) -> str:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = parse_status(content)
        # A (re)written plan always waits for approval again; only the user's
        # explicit approval moves it to EXECUTING.
        if status != WAITING:
            content = set_status(content, WAITING)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return f"Plan saved to {path}. Stop now and wait for the user to approve or revise it."
