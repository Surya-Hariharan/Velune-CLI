"""TaskTemplateRegistry — see docs/02-prompt-intelligence.md §4, §10.

Role fragments must come from velune.cognition.prompts wherever an
equivalent council seat exists; this registry must never re-author prompt
text that already lives there.
"""

from __future__ import annotations

from velune.cognition.prompts import COUNCIL_CODER, COUNCIL_PLANNER, COUNCIL_REVIEWER, get_prompt
from velune.prompt_intelligence.intent import TaskKind
from velune.prompt_intelligence.ir import Constraint, PlanningStyle, WorkflowSpec
from velune.prompt_intelligence.templates import TaskTemplate, TaskTemplateRegistry


def test_registry_has_a_template_for_every_task_kind():
    registry = TaskTemplateRegistry()
    for kind in TaskKind:
        template = registry.get(kind)
        assert template.kind == kind
        assert template.role_fragment.strip()


def test_coding_template_reuses_council_coder_prompt_verbatim():
    registry = TaskTemplateRegistry()
    template = registry.get(TaskKind.CODING)
    assert template.role_fragment == get_prompt(COUNCIL_CODER)


def test_planning_template_reuses_council_planner_prompt_verbatim():
    registry = TaskTemplateRegistry()
    template = registry.get(TaskKind.PLANNING)
    assert template.role_fragment == get_prompt(COUNCIL_PLANNER)


def test_code_review_template_reuses_council_reviewer_prompt_verbatim():
    registry = TaskTemplateRegistry()
    template = registry.get(TaskKind.CODE_REVIEW)
    assert template.role_fragment == get_prompt(COUNCIL_REVIEWER)


def test_register_overrides_a_template():
    registry = TaskTemplateRegistry()
    custom = TaskTemplate(
        kind=TaskKind.RESEARCH,
        role_fragment="Custom research persona.",
        constraints=(Constraint(text="Cite sources.", source="template"),),
        workflow=WorkflowSpec(style=PlanningStyle.LIGHT_TOUCH, steps=("search", "summarize")),
    )
    registry.register(TaskKind.RESEARCH, custom)
    assert registry.get(TaskKind.RESEARCH) is custom


def test_registries_are_independent_instances():
    # Mutating one registry must not leak into another (no shared mutable
    # module-level state between instances).
    a = TaskTemplateRegistry()
    b = TaskTemplateRegistry()
    a.register(
        TaskKind.RESEARCH,
        TaskTemplate(kind=TaskKind.RESEARCH, role_fragment="A-only override."),
    )
    assert b.get(TaskKind.RESEARCH).role_fragment != "A-only override."
