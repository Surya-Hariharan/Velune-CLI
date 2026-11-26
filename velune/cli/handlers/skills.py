"""``/skills`` — list and show the council's skills and permission profiles."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.table import Table

from velune.cli import design
from velune.swarm.library import SkillLibrary

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL


def load_library(repl: VeluneREPL) -> SkillLibrary:
    """The library for the REPL's workspace (project files only when trusted)."""
    from velune.core import trust

    workspace = Path(str(repl.container.get("runtime.workspace") or Path.cwd()))
    manager = getattr(repl, "_plugin_manager", None)
    plugin_skills = manager.all_skills() if manager is not None else []
    return SkillLibrary.load(
        workspace=workspace, trusted=trust.is_trusted(workspace), plugin_skills=plugin_skills
    )


def _flow_text(stages: tuple[tuple[str, ...], ...]) -> str:
    return " → ".join(", ".join(stage) for stage in stages)


async def cmd_skills(repl: VeluneREPL, args: str) -> None:
    library = load_library(repl)
    words = args.split()
    if words[:1] == ["show"] and len(words) == 2:
        _show(repl, library, words[1])
        return
    if words:
        repl.console.print("[yellow]Usage: /skills  or  /skills show <name>[/yellow]")
        return

    skills = Table(title="Skills", box=None, header_style=f"bold {design.ACCENT}")
    for column in ("Name", "Team (in order)", "Output", "Default permission", "From"):
        skills.add_column(column)
    for skill in library.skills.values():
        skills.add_row(
            skill.slug,
            _flow_text(skill.flow),
            skill.output,
            skill.default_permission,
            skill.source,
        )
    profiles = Table(title="Permission profiles", box=None, header_style=f"bold {design.ACCENT}")
    for column in ("Name", "Tools", "Writes", "Commands", "MCP", "From"):
        profiles.add_column(column)
    for profile in library.profiles.values():
        profiles.add_row(
            profile.slug,
            ", ".join(sorted(profile.tools)) or "none",
            profile.write,
            profile.commands,
            ", ".join(profile.mcp) or "none",
            profile.source,
        )
    repl.console.print(skills)
    repl.console.print()
    repl.console.print(profiles)
    for warning in library.warnings:
        repl.console.print(f"[yellow]Skipped:[/yellow] [dim]{warning}[/dim]")
    repl.console.print(
        "[dim]/skills show <name> for details · add your own in ~/.velune/skills/ "
        "and ~/.velune/permissions/[/dim]"
    )


def _show(repl: VeluneREPL, library: SkillLibrary, name: str) -> None:
    skill, profile = library.skill(name), library.profile(name)
    if skill is not None:
        repl.console.print(
            f"[bold]{skill.name}[/bold] [dim]({skill.slug}, {skill.source})[/dim]\n"
            f"{skill.description}\n"
            f"[dim]Team:[/dim] {_flow_text(skill.flow)}   [dim]Lead:[/dim] {skill.lead}   "
            f"[dim]Writers:[/dim] {', '.join(skill.writers) or 'none'}\n"
            f"[dim]Output:[/dim] {skill.output}   "
            f"[dim]Default permission:[/dim] {skill.default_permission}"
        )
        repl.console.print(Markdown(skill.body))
        for role in skill.team:
            repl.console.print(Markdown(f"### {role}\n\n{skill.roles[role]}"))
        return
    if profile is not None:
        repl.console.print(
            f"[bold]{profile.name}[/bold] [dim]({profile.slug}, {profile.source})[/dim]\n"
            f"{profile.description}\n"
            f"[dim]Tools:[/dim] {', '.join(sorted(profile.tools)) or 'none'}   "
            f"[dim]Writes:[/dim] {profile.write}   [dim]Commands:[/dim] {profile.commands}   "
            f"[dim]MCP:[/dim] {', '.join(profile.mcp) or 'none'}   "
            f"[dim]Max tool calls:[/dim] {profile.max_tool_calls}"
        )
        repl.console.print(Markdown(profile.body))
        return
    repl.console.print(f"[yellow]No skill or permission profile named '{name}'.[/yellow]")
