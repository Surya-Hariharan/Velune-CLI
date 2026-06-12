"""Production PlannerAgent with budget enforcement and state isolation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import PlannerMessage
from velune.core.types.model import ModelDescriptor
from velune.core.types.task import TaskPlan, TaskStatus, TaskStep
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

if TYPE_CHECKING:
    from velune.cognition.state import CouncilState

logger = logging.getLogger("velune.cognition.agents.planner")

PLANNER_SYSTEM_PROMPT = """You are the Lead Planner for the Velune Reasoning Council.
Your role is to translate the user request and repository context into a strictly structured ExecutionPlan DAG.

Decompose complex workflows into small, sequential execution steps.
Each step should specify:
1. 'id': Unique lowercase alpha-numeric string (e.g. 'setup_env', 'write_code', 'run_tests').
2. 'description': Concise summary of what the step achieves.
3. 'target_files': List of files this step targets/modifies.
4. 'expected_outcome': Expected state after step completion.

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "task_id": "<alphanumeric_id>",
  "steps": [
    {
      "id": "step_1",
      "description": "Create hello.py",
      "target_files": ["hello.py"],
      "expected_outcome": "File created with correct syntax",
      "agent_role": "coder"
    }
  ]
}
"""


class PlannerAgent(BaseCouncilAgent):
    """Production Planner Agent with budget enforcement."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.PLANNER,
            model=model,
            provider=provider,
            system_prompt=PLANNER_SYSTEM_PROMPT,
        )

    async def generate_plan(
        self,
        task: str,
        retrieved_context: str,
        state: CouncilState,
    ) -> TaskPlan:
        """Generate task plan with timeout enforcement from budget.

        Args:
            task: Original task description
            retrieved_context: Repository context + architectural drift alarms
            state: CouncilState to write plan into

        Returns:
            TaskPlan with decomposed steps

        Raises:
            TimeoutError: If planner_timeout_seconds exceeded
            ValueError: If budget exhausted before execution
        """
        if state.is_budget_exhausted():
            raise ValueError(f"Wall-clock budget exhausted before Planner could run")

        remaining = state.remaining_budget_seconds()
        timeout = min(state.budget.planner_timeout_seconds, int(remaining))

        logger.info(
            "Planner starting plan generation (timeout: %ds, wall budget: %.1fs remaining)",
            timeout, remaining
        )

        try:
            import asyncio

            # Wrap in timeout guard
            user_messages = [
                {
                    "role": "user",
                    "content": f"TASK: {task}\n\nCONTEXT & WARNINGS:\n{retrieved_context}",
                }
            ]

            result = await asyncio.wait_for(
                self.typed_deliberate(user_messages, PlannerMessage, temperature=0.2),
                timeout=timeout,
            )

            if result.parse_error:
                logger.error("Planner parse failed: %s", result.parse_error)
                task_plan = self._create_fallback_plan(task)
            else:
                steps = []
                for step_dict in result.steps:
                    steps.append(
                        TaskStep(
                            id=step_dict.get("id", "step-unknown"),
                            description=step_dict.get("description", ""),
                            agent_role=step_dict.get("agent_role", "coder"),
                            status=TaskStatus.PENDING,
                            dependencies=step_dict.get("dependencies", []),
                            metadata={
                                "target_files": step_dict.get("target_files", []),
                                "expected_outcome": step_dict.get("expected_outcome", ""),
                            },
                        )
                    )

                task_plan = TaskPlan(
                    task_id=result.task_id or "task-main",
                    steps=steps,
                )

            state.set_planner_output(task_plan, retrieved_context)
            logger.info("Planner completed plan with %d steps", len(task_plan.steps))
            return task_plan

        except asyncio.TimeoutError:
            logger.error("Planner timed out after %ds", timeout)
            task_plan = self._create_fallback_plan(task)
            state.set_planner_output(task_plan, retrieved_context)
            raise

    def _create_fallback_plan(self, task: str) -> TaskPlan:
        """Create minimal single-step fallback plan."""
        fallback_step = TaskStep(
            id="execute_goal",
            description=f"Complete goal: {task}",
            agent_role="coder",
            status=TaskStatus.PENDING,
            metadata={
                "target_files": [],
                "expected_outcome": "Task completed",
            },
        )
        return TaskPlan(
            task_id="task-fallback",
            steps=[fallback_step],
        )
