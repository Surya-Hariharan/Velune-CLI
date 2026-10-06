"""Plan-mode turn flow: plan → (revise)* → explicit approval → execute → report.

``before_turn`` decides what this message means in PLAN mode (a new task, a
revision, or the approval) and tells the model through a system message.
``after_turn`` records a plan the model wrote, shows its summary and waits —
or, after an execution turn, writes the results into the plan and marks it
DONE. What the model may *do* is enforced by the permission policy, not by
these messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from velune.permissions import ExecutionMode
from velune.permissions.plans import PLAN_TEMPLATE, PlanManager, is_approval

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL


def plan_manager(repl: VeluneREPL) -> PlanManager:
    pm = getattr(repl, "_plan_manager", None)
    if pm is None:
        workspace = None
        try:
            workspace = repl.container.get("runtime.workspace")
        except Exception:
            pass
        pm = PlanManager(Path(workspace) if workspace else Path.cwd())
        repl._plan_manager = pm
    return pm


def _rel(pm: PlanManager, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(pm.workspace))
    except ValueError:
        return str(path)


_INSTRUCTION_PREFIXES = (
    "PLAN MODE.",
    "The user APPROVED the plan",
    "The user is REVISING the plan",
)


def drop_stale_instructions(conversation: list[dict]) -> None:
    """Remove plan instructions left by a turn that ended early (error, Ctrl+C).

    Matched by their fixed opening text rather than an extra marker key, since
    unknown message keys get sent to (and rejected by) strict providers.
    """
    for index in range(len(conversation) - 1, -1, -1):
        message = conversation[index]
        content = message.get("content") if isinstance(message, dict) else None
        if (
            message.get("role") == "system"
            and isinstance(content, str)
            and content.startswith(_INSTRUCTION_PREFIXES)
        ):
            del conversation[index]


def before_turn(repl: VeluneREPL, text: str) -> str | None:
    """Return a system instruction for this turn (or None outside PLAN mode)."""
    if getattr(repl, "_execution_mode", None) is not ExecutionMode.PLAN:
        return None
    pm = plan_manager(repl)
    pm.begin_turn()
    if pm.waiting and is_approval(text):
        pm.approve()
        repl.console.print(
            f"[green]✓ Plan approved[/green] [dim]— executing {_rel(pm, pm.active)}. "
            "Changes outside the plan's files will ask first.[/dim]"
        )
        return (
            f"The user APPROVED the plan at {pm.active}. Execute it now: read it, follow "
            "its Execution Order, make the changes with the tools, run its tests/validation, "
            "and fix failures. Only the files in its Files Affected table are pre-approved; "
            "anything else needs the user's approval. Finish with a short report of what "
            "was done and the validation results."
        )
    if pm.waiting:
        return (
            f"The user is REVISING the plan at {pm.active} (it has not been approved). "
            "Read the plan, apply the requested changes, save it again with write_plan "
            f"using name '{pm.active.stem if pm.active else 'plan'}', then stop. Do not "
            "change any project files."
        )
    return (
        "PLAN MODE. Investigate with read-only tools (read files, search, git status/diff, "
        "read-only commands). Then you MUST save the plan by calling the write_plan tool "
        "(do not just print it), using exactly these sections, and stop — do not change "
        "project files:\n" + PLAN_TEMPLATE
    )


def looks_like_plan(text: str) -> bool:
    """A plan printed as text: at least two of the template's main sections."""
    lowered = text.lower()
    markers = ("## files affected", "## proposed changes", "## execution order", "## objective")
    return sum(marker in lowered for marker in markers) >= 2


def save_text_plan(pm: PlanManager, text: str) -> Path:
    """Save a plan the model printed (instead of calling write_plan) as the active plan."""
    import re

    from velune.permissions.plans import WAITING, parse_status, set_status, slugify

    title = re.search(r"^##\s*Objective\s*\n+(.+)$", text, re.M | re.I)
    name = slugify(title.group(1)[:50] if title else "plan")
    path = pm.plans_dir / f"{name}.md"
    pm.plans_dir.mkdir(parents=True, exist_ok=True)
    body = text if parse_status(text) == WAITING else set_status(text, WAITING)
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return path


def after_turn(repl: VeluneREPL, assistant_text: str) -> None:
    if getattr(repl, "_execution_mode", None) is not ExecutionMode.PLAN:
        return
    pm = plan_manager(repl)
    if pm.executing:
        pm.finish(assistant_text)
        repl.console.print(
            f"[green]Plan complete[/green] [dim]— results written to {_rel(pm, pm.active)} "
            "(status DONE). Describe the next task to plan another.[/dim]"
        )
        return
    written = pm.newest_written_this_turn()
    if written is None and pm.active is None and looks_like_plan(assistant_text):
        # The model printed the plan instead of calling write_plan. Velune saves
        # it itself (its own plans dir) so the approve/revise flow still works.
        written = save_text_plan(pm, assistant_text)
    if written is None:
        return
    revised = pm.active is not None and written == pm.active
    pm.record_new(written)
    counts = pm.summary()
    verb = "updated" if revised else "created"
    repl.console.print(
        f"[bold]Plan {verb}:[/bold] {_rel(pm, written)}\n"
        f"  Modify {counts['modify']} · Create {counts['create']} · "
        f"Delete {counts['delete']} · Tests {counts['tests']}\n\n"
        "[bold]Waiting for approval.[/bold] "
        "[dim]Reply [bold]approve[/bold] to execute, or describe changes to revise.[/dim]"
    )
