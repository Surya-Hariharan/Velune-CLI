"""Rich terminal visualization of the Reasoning Council debate, scoring, and votes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from velune.cli import design

if TYPE_CHECKING:
    from velune.cognition.council.planner import TaskPlan
    from velune.models.specializations import CouncilRole


class CouncilDisplayView:
    """Beautiful Rich-based UI components to visualize Reasoning Council deliberations."""

    def __init__(self, console: Console) -> None:
        self.console = console

    def render_header(self, task: str) -> None:
        """Display an eye-catching header for the council run."""
        self.console.print()
        # Parse the static markup labels, then append `task` as literal
        # styled text (not through markup parsing) — task is user-controlled
        # and may itself contain '[' characters that would otherwise be
        # misread as markup tags.
        body = Text.from_markup(
            f"[bold {design.ACCENT}]VELUNE COGNITIVE OS[/bold {design.ACCENT}]"
            f" — [{design.INFO}]Reasoning Council Active[/{design.INFO}]\n"
            f"[{design.MUTED}]Objective:[/{design.MUTED}] "
        )
        body.append(task, style="italic white")
        self.console.print(
            Panel(
                body,
                border_style=design.ACCENT,
                box=ROUNDED,
                title="[bold white]Cognitive Deliberation[/bold white]",
                title_align="left",
            )
        )

    def render_role_assignments(self, assignments: dict[CouncilRole, Any]) -> None:
        """Render a table displaying mapped specialized models for the council."""
        table = Table(
            title=f"[bold {design.INFO}]Mapped Council Specializations[/bold {design.INFO}]",
            box=ROUNDED,
            border_style=design.FAINT,
            expand=True,
        )
        table.add_column("Council Seat", style=f"bold {design.HIGHLIGHT}")
        table.add_column("Provider / Endpoint", style=design.OK)
        table.add_column("Target Model", style=design.INFO)
        table.add_column("Key Skills / Tags", style=design.ACCENT_SOFT)

        for role, desc in assignments.items():
            caps = []
            if hasattr(desc, "capabilities") and desc.capabilities:
                for cap_name in ["coding", "reasoning", "planning", "summarization", "tool_use"]:
                    level = getattr(desc.capabilities, cap_name, None)
                    if level and level > 0:
                        caps.append(f"{cap_name} ({level.name})")
            tags = (
                ", ".join(caps)
                if caps
                else ", ".join(desc.tags)
                if getattr(desc, "tags", None)
                else "reasoning"
            )
            table.add_row(role.value.upper(), desc.provider_id.capitalize(), desc.model_id, tags)
        self.console.print(table)
        self.console.print()

    def render_step_header(self, step_name: str, agent_emoji: str = "") -> None:
        """Draw an elegant boundary indicating a change in agent active deliberation."""
        label = f"{agent_emoji} {step_name}".strip() if agent_emoji else step_name
        self.console.print(
            f"\n[bold {design.ACCENT}]>[/bold {design.ACCENT}]"
            f" [bold white]{label}[/bold white] is deliberating..."
        )

    def render_planner_dag(self, plan: TaskPlan) -> None:
        """Render the Planner's task plan DAG as a neat hierarchical or sequential table."""
        table = Table(
            title=f"[bold {design.HIGHLIGHT}]Execution Plan Compiled by Council Planner[/bold {design.HIGHLIGHT}]",
            box=ROUNDED,
            border_style=design.HIGHLIGHT,
            expand=True,
        )
        table.add_column("ID", style=f"bold {design.INFO}", width=8)
        table.add_column("Description", style="white")
        table.add_column("Dependencies", style=design.ACCENT_SOFT)
        table.add_column("Validation Strategy", style=design.OK)

        for step in plan.steps:
            deps = ", ".join(step.dependencies) if step.dependencies else "[dim]None[/dim]"
            val = step.metadata.get("test_command") or "Syntax check + File existence"
            table.add_row(step.id, step.description, deps, str(val))
        self.console.print(table)
        self.console.print()

    def render_code_proposal(self, code_proposal: str) -> None:
        """Format the coder's proposed implementation code inside a syntax-focused block."""
        self.console.print(
            Panel(
                Text(code_proposal, style=design.OK),
                title=f"[bold {design.OK}]Coder Proposed Patch[/bold {design.OK}]",
                border_style=design.OK,
                box=ROUNDED,
                expand=True,
            )
        )

    def render_reviewer_report(self, report: Any) -> None:
        """Render the Reviewer's static audit, showing passed status and critical issues."""
        if report is None:
            # A legitimate result, not an error: lower council tiers skip the
            # Reviewer phase entirely (see CouncilOrchestrator's tier<3 early
            # return), and a Reviewer deliberation that fails outright (e.g. a
            # decommissioned model) can also surface as None from the runner.
            self.console.print(
                f"[{design.MUTED}]Reviewer did not run for this tier/request.[/{design.MUTED}]"
            )
            return
        if isinstance(report, dict):
            passed = report.get("passed", True)
            confidence = report.get("confidence_rating", 0.8)
            issues = report.get("critical_issues", [])
        else:
            passed = report.passed
            confidence = report.confidence_rating
            issues = report.critical_issues

        ok_c = design.OK
        err_c = design.DANGER
        status_text = (
            f"[bold {ok_c}]PASS[/bold {ok_c}]"
            if passed
            else f"[bold {err_c}]FAIL / BLOCKED[/bold {err_c}]"
        )
        border_style = design.OK if passed else design.DANGER

        content = []
        content.append(f"[bold]Verification Status:[/bold] {status_text}")
        content.append(f"[bold]Confidence Rating:[/bold] {confidence:.2f}")

        if issues:
            content.append(f"\n[bold {err_c}]Critical Issues Detected:[/bold {err_c}]")
            for issue in issues:
                content.append(f"  [{err_c}]{issue}[/{err_c}]")
        else:
            content.append(
                f"\n[{ok_c}]Static checks passed. No syntactical or safety concerns raised.[/{ok_c}]"
            )

        self.console.print(
            Panel(
                "\n".join(content),
                title="[bold]Reviewer Audit[/bold]",
                border_style=border_style,
                box=ROUNDED,
                expand=True,
            )
        )

    def render_challenger_report(self, report: Any) -> None:
        """Render the Challenger's adversarial audit and failure vector probes."""
        if report is None:
            # See render_reviewer_report — None is a legitimate "did not run
            # for this tier/request" state, not necessarily an error.
            self.console.print(
                f"[{design.MUTED}]Challenger did not run for this tier/request.[/{design.MUTED}]"
            )
            return
        if isinstance(report, dict):
            severity = report.get("severity_rating", 0.0)
            vectors = report.get("failure_vectors", [])
        else:
            severity = report.severity_rating
            vectors = report.failure_vectors

        warn_c = design.WARN
        border_style = warn_c if severity > 0.4 else design.FAINT

        content = []
        content.append(
            f"[bold]Adversarial Severity Rating:[/bold] [bold {design.DANGER}]{severity:.2f}[/bold {design.DANGER}] / 1.00"
        )

        if vectors:
            content.append(f"\n[bold {warn_c}]Failure Vectors Simulated:[/bold {warn_c}]")
            for vec in vectors:
                content.append(f"  [{warn_c}]{vec}[/{warn_c}]")
        else:
            content.append(
                f"\n[{design.MUTED}]No significant failure vectors or edge-case gaps simulated.[/{design.MUTED}]"
            )

        self.console.print(
            Panel(
                "\n".join(content),
                title="[bold]Challenger Adversarial Check[/bold]",
                border_style=border_style,
                box=ROUNDED,
                expand=True,
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

        ok_c = design.OK
        warn_c = design.WARN
        err_c = design.DANGER
        if confidence > 0.75:
            conf_str = f"[bold {ok_c}]{confidence * 100:.1f}% (High Confidence)[/bold {ok_c}]"
            border_style = ok_c
        elif confidence > 0.55:
            conf_str = f"[bold {warn_c}]{confidence * 100:.1f}% (Medium Confidence)[/bold {warn_c}]"
            border_style = warn_c
        else:
            conf_str = f"[bold {err_c}]{confidence * 100:.1f}% (Low Confidence / High Volatility)[/bold {err_c}]"
            border_style = err_c

        status_text = (
            f"[bold {err_c}]YES (Blocked / Escalate)[/bold {err_c}]"
            if review_required
            else f"[bold {ok_c}]NO (Autonomous Pass)[/bold {ok_c}]"
        )

        content = []
        content.append(f"[bold]Calibrated Council Confidence Score:[/bold] {conf_str}")
        content.append(f"[bold]Escalate to Human-in-the-Loop Review:[/bold] {status_text}")

        if flags:
            content.append(f"[bold {err_c}]System Flags Raised:[/bold {err_c}] {', '.join(flags)}")

        if winning_claims:
            content.append(
                f"\n[bold {design.INFO}]Winning Claims & Arbitration Compromise:[/bold {design.INFO}]"
            )
            for claim in winning_claims:
                content.append(f"  [{design.INFO}]{claim}[/{design.INFO}]")

        if synthesis_inst:
            content.append(
                f"\n[bold {design.MUTED}]Arbitrator Instructions for Synthesizer:[/bold {design.MUTED}]"
            )
            content.append(f"[{design.MUTED}]{synthesis_inst}[/{design.MUTED}]")

        self.console.print(
            Panel(
                "\n".join(content),
                title="[bold white]Council Arbitration Engine[/bold white]",
                border_style=border_style,
                box=ROUNDED,
                expand=True,
            )
        )

    def render_synthesized_response(self, text: str) -> None:
        """Display final walker walkthrough summary and accomplishments."""
        self.console.print(
            Panel(
                Text(text, style="white"),
                title=f"[bold {design.ACCENT}]Deliberated Walkthrough & Accomplishments[/bold {design.ACCENT}]",
                border_style=design.ACCENT,
                box=ROUNDED,
                expand=True,
            )
        )


def render_phase_timing_footer(console: Console, timings: dict[str, float]) -> None:
    """Render a compact phase-timing summary table after a council run."""
    if len(timings) < 2:
        return
    table = Table(
        title="Phase Timings",
        box=ROUNDED,
        show_header=True,
        border_style=design.MUTED,
        title_style=f"dim {design.MUTED}",
        padding=(0, 1),
        expand=False,
    )
    table.add_column("Phase", style=design.INFO, no_wrap=True)
    table.add_column("Elapsed", style=design.MUTED, justify="right")
    for phase, ms in timings.items():
        table.add_row(phase.capitalize(), f"{ms / 1000:.1f}s")
    console.print(table)
