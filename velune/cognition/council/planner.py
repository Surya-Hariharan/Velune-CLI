"""Planner agent decomposing user intents into structured ExecutionPlans."""

from __future__ import annotations

import logging

from velune.cognition.council.base import BaseCouncilAgent
from velune.cognition.council.messages import PlannerMessage
from velune.core.types.model import ModelDescriptor
from velune.core.types.task import TaskPlan, TaskStatus, TaskStep
from velune.models.specializations import CouncilRole
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.cognition.council.planner")

PLANNER_SYSTEM_PROMPT = """You are the Lead Planner for the Velune Reasoning Council.
Your role is to translate the user request and repository context into a strictly structured ExecutionPlan DAG.

Decompose complex workflows into small, sequential execution steps.
Each step should specify:
1. 'id': Unique lowercase alpha-numeric string (e.g. 'setup_env', 'write_code', 'run_tests').
2. 'description': Concise summary of what the step achieves.
3. 'agent_role': Council agent executing this ('coder', 'reviewer', etc).
4. 'dependencies': List of step IDs that MUST complete before this step can begin.
5. 'metadata': A dictionary containing execution detail:
   - 'command': The exact command string to run in the isolated subprocess sandbox.
   - 'expected_files': List of file paths relative to workspace that must be created or modified.
   - 'syntax_check_files': List of file paths to run language-specific syntax compiler checks against.
    - 'test_command': Optional validation command to run for local checks.
   - 'timeout': Max seconds to allow this command to run before failing (default 60.0).

OUTPUT EXCLUSIVELY A RAW VALID JSON OBJECT WITH NO CODEBLOCK WRAPPERS OR Markdown.
JSON Format:
{
  "task_id": "<alphanumeric_id>",
  "steps": [
    {
      "id": "step_1",
      "description": "Create hello.py",
      "agent_role": "coder",
      "dependencies": [],
      "metadata": {
        "command": "echo print('Hello') > hello.py",
        "expected_files": ["hello.py"],
        "syntax_check_files": ["hello.py"],
        "timeout": 30.0
      }
    }
  ]
}
"""


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
                logger.error("Failed to parse Planner JSON output. Falling back to default single step. Error: %s", result.parse_error)
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
            logger.error("Exception during generate_plan: %s. Falling back to default single step.", e)
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
