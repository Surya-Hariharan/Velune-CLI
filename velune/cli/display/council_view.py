"""Rich terminal visualization of the Reasoning Council debate, scoring, and votes."""

from __future__ import annotations

from typing import Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from velune.cognition.council.planner import TaskPlan
from velune.models.specializations import CouncilRole


class CouncilDisplayView:
    """Beautiful Rich-based UI components to visualize Reasoning Council deliberations."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def render_header(self, task: str) -> None:
        """Display an eye-catching header for the council run."""
        self.console.print()
        self.console.print(
            Panel(
                Text.assemble(
                    ("[bold magenta]VELUNE COGNITIVE OS[/bold magenta] — [cyan]Reasoning Council Active[/cyan]\n"),
                    ("[dim]Objective:[/dim] ", "[italic white]" + task + "[/italic white]")
                ),
                border_style="magenta",
                box=ROUNDED,
                title="[bold white]🧠 Cognitive Deliberation[/bold white]",
                title_align="left"
            )
        )

    def render_role_assignments(self, assignments: dict[CouncilRole, Any]) -> None:
        """Render a table displaying mapped specialized models for the council."""
        table = Table(
            title="[bold cyan]Mapped Council Specializations[/bold cyan]",
            box=ROUNDED,
            border_style="dim",
            expand=True
        )
        table.add_column("Council Seat", style="bold yellow")
        table.add_column("Provider / Endpoint", style="green")
        table.add_column("Target Model", style="cyan")
        table.add_column("Key Skills / Tags", style="magenta")

        for role, desc in assignments.items():
            caps = []
            if hasattr(desc, "capabilities") and desc.capabilities:
                for cap_name in ["coding", "reasoning", "planning", "summarization", "tool_use"]:
                    level = getattr(desc.capabilities, cap_name, None)
                    if level and level > 0:
                        caps.append(f"{cap_name} ({level.name})")
            tags = ", ".join(caps) if caps else ", ".join(desc.tags) if getattr(desc, "tags", None) else "reasoning"
            table.add_row(
                role.value.upper(),
                desc.provider_id.capitalize(),
                desc.model_id,
                tags
            )
        self.console.print(table)
        self.console.print()

    def render_step_header(self, step_name: str, agent_emoji: str = "🤖") -> None:
        """Draw an elegant boundary indicating a change in agent active deliberation."""
        self.console.print(f"\n[bold magenta]●[/bold magenta] [bold white]{agent_emoji} {step_name}[/bold white] is deliberating...")

    def render_planner_dag(self, plan: TaskPlan) -> None:
        """Render the Planner's task plan DAG as a neat hierarchical or sequential table."""
        table = Table(
            title="[bold yellow]Execution Plan Compiled by Council Planner[/bold yellow]",
            box=ROUNDED,
            border_style="yellow",
            expand=True
        )
        table.add_column("ID", style="bold cyan", width=8)
        table.add_column("Description", style="white")
        table.add_column("Dependencies", style="magenta")
        table.add_column("Validation Strategy", style="green")

        for step in plan.steps:
            deps = ", ".join(step.dependencies) if step.dependencies else "[dim]None[/dim]"
            val = step.metadata.get("test_command") or "Syntax check + File existence"
            table.add_row(
                step.id,
                step.description,
                deps,
                str(val)
            )
        self.console.print(table)
        self.console.print()

    def render_code_proposal(self, code_proposal: str) -> None:
        """Format the coder's proposed implementation code inside a syntax-focused block."""
        self.console.print(
            Panel(
                Text(code_proposal, style="green"),
                title="[bold green]💻 Coder Proposed Patch[/bold green]",
                border_style="green",
                box=ROUNDED,
                expand=True
            )
        )

    def render_reviewer_report(self, report: Any) -> None:
        """Render the Reviewer's static audit, showing passed status and critical issues."""
        if isinstance(report, dict):
            passed = report.get("passed", True)
            confidence = report.get("confidence_rating", 0.8)
            issues = report.get("critical_issues", [])
        else:
            passed = report.passed
            confidence = report.confidence_rating
            issues = report.critical_issues

        status_text = "[bold green]PASS[/bold green]" if passed else "[bold red]FAIL / BLOCKED[/bold red]"
        border_style = "green" if passed else "red"

        content = []
        content.append(f"[bold]Verification Status:[/bold] {status_text}")
        content.append(f"[bold]Confidence Rating:[/bold] {confidence:.2f}")

        if issues:
            content.append("\n[bold red]⚠️ Critical Issues Detected:[/bold red]")
            for issue in issues:
                content.append(f"  [red]•[/red] {issue}")
        else:
            content.append("\n[green]✓ Static static checks passed. No syntactical or safety concerns raised.[/green]")

        self.console.print(
            Panel(
                "\n".join(content),
                title="[bold]🔍 Reviewer Audit[/bold]",
                border_style=border_style,
                box=ROUNDED,
                expand=True
            )
        )

    def render_challenger_report(self, report: Any) -> None:
        """Render the Challenger's adversarial audit and failure vector probes."""
        if isinstance(report, dict):
            severity = report.get("severity_rating", 0.0)
            vectors = report.get("failure_vectors", [])
        else:
            severity = report.severity_rating
            vectors = report.failure_vectors

        border_style = "yellow" if severity > 0.4 else "dim"

        content = []
        content.append(f"[bold]Adversarial Severity Rating:[/bold] [bold red]{severity:.2f}[/bold red] / 1.00")

        if vectors:
            content.append("\n[bold yellow]⚡ Failure Vectors Simulated:[/bold yellow]")
            for vec in vectors:
                content.append(f"  [yellow]•[/yellow] {vec}")
        else:
            content.append("\n[dim]No significant failure vectors or edge-case gaps simulated.[/dim]")

        self.console.print(
            Panel(
                "\n".join(content),
                title="[bold]⚡ Challenger Adversarial Check[/bold]",
                border_style=border_style,
                box=ROUNDED,
                expand=True
            )
        )

    def render_arbitration_result(self, res: Any) -> None:
        """Display calibrated confidence score, contradiction matches, and human-review flags."""
        if isinstance(res, dict):
            confidence = res.get("overall_confidence", 0.8)
            review_required = res.get("requires_human_review", False)
            flags = res.get("flags", [])
            winning_claims = res.get("winning_claims", [])
            synthesis_inst = res.get("synthesis_instructions", "")
        else:
            confidence = res.overall_confidence
            review_required = res.requires_human_review
            flags = res.flags
            winning_claims = res.winning_claims
            synthesis_inst = res.synthesis_instructions

        # Color calibrated confidence based on score
        if confidence > 0.75:
            conf_str = f"[bold green]{confidence * 100:.1f}% (High Confidence)[/bold green]"
            border_style = "green"
        elif confidence > 0.55:
            conf_str = f"[bold yellow]{confidence * 100:.1f}% (Medium Confidence)[/bold yellow]"
            border_style = "yellow"
        else:
            conf_str = f"[bold red]{confidence * 100:.1f}% (Low Confidence / High Volatility)[/bold red]"
            border_style = "red"

        status_text = "[bold red]YES (Blocked / Escalate)[/bold red]" if review_required else "[bold green]NO (Autonomous Pass)[/bold green]"

        content = []
        content.append(f"[bold]Calibrated Council Confidence Score:[/bold] {conf_str}")
        content.append(f"[bold]Escalate to Human-in-the-Loop Review:[/bold] {status_text}")

        if flags:
            content.append(f"[bold red]System Flags Raised:[/bold red] {', '.join(flags)}")

        if winning_claims:
            content.append("\n[bold cyan]Winning Claims & Arbitration Compromise:[/bold cyan]")
            for claim in winning_claims:
                content.append(f"  [cyan]✓[/cyan] {claim}")

        if synthesis_inst:
            content.append("\n[bold dim]Arbitrator Instructions for Synthesizer:[/bold dim]")
            content.append(f"[dim]{synthesis_inst}[/dim]")

        self.console.print(
            Panel(
                "\n".join(content),
                title="[bold white]⚖️ Council Arbitration Engine[/bold white]",
                border_style=border_style,
                box=ROUNDED,
                expand=True
            )
        )

    def render_synthesized_response(self, text: str) -> None:
        """Display final walker walkthrough summary and accomplishments."""
        self.console.print(
            Panel(
                Text(text, style="white"),
                title="[bold magenta]🚀 Deliberated Walkthrough & Accomplishments[/bold magenta]",
                border_style="magenta",
                box=ROUNDED,
                expand=True
            )
        )
