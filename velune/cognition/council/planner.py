"""Planner agent decomposing user intents into structured ExecutionPlans."""

from __future__ import annotations

import logging

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import PlannerMessage
from velune.cognition.prompts import COUNCIL_PLANNER, get_prompt
from velune.core.types.model import ModelDescriptor
from velune.core.types.task import TaskPlan, TaskStatus, TaskStep
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council.planner")

PLANNER_SYSTEM_PROMPT = get_prompt(COUNCIL_PLANNER)


class PlannerAgent(BaseCouncilAgent):
    """Planner Agent producing structured execution DAGs."""

    def __init__(self, model: ModelDescriptor, provider: ModelProvider) -> None:
        super().__init__(
            role=CouncilRole.PLANNER,
            model=model,
            provider=provider,
            system_prompt=PLANNER_SYSTEM_PROMPT,
        )

    async def generate_plan(self, prompt: str, repo_context: str) -> TaskPlan:
        """Analyze goals and emit a verified TaskPlan."""
        logger.info("Planner generating execution plan...")

        try:
            user_messages = [
                {
                    "role": "user",
                    "content": f"USER PROMPT: {prompt}\n\nREPOSITORY DETAILS AND CONTEXT:\n{repo_context}",
                }
            ]

            result = await self.typed_deliberate(user_messages, PlannerMessage, temperature=0.2)

            if result.parse_error:
                logger.error(
                    "Failed to parse Planner JSON output. Falling back to default single step. Error: %s",
                    result.parse_error,
                )
                # Create a fallback single-step plan
                return self._create_fallback_plan(prompt)

            steps = []
            for s in result.steps:
                steps.append(
                    TaskStep(
                        id=s.get("id", "step-unknown"),
                        description=s.get("description", ""),
                        agent_role=s.get("agent_role", "coder"),
                        status=TaskStatus.PENDING,
                        dependencies=s.get("dependencies", []),
                        metadata=s.get("metadata", {}),
                    )
                )

            return TaskPlan(
                task_id=result.task_id or "task-main",
                steps=steps,
            )
        except Exception as e:
            logger.error(
                "Exception during generate_plan: %s. Falling back to default single step.", e
            )
            return self._create_fallback_plan(prompt)

    def _create_fallback_plan(self, prompt: str) -> TaskPlan:
        fallback_step = TaskStep(
            id="execute_goal",
            description=f"Attempt to complete goal: {prompt}",
            agent_role="coder",
            status=TaskStatus.PENDING,
            metadata={
                "command": f"echo Running fallbacks for: {prompt}",
                "timeout": 60.0,
            },
        )
        return TaskPlan(
            task_id="task-fallback",
            steps=[fallback_step],
        )
