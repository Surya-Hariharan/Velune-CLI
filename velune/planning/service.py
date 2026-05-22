"""Adaptive planning and replanning service."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from velune.core.types.task import TaskPlan, TaskStatus, TaskStep


class AdaptivePlanningService:
    """Builds and revises hierarchical engineering execution plans."""

    def create_plan(
        self,
        task_id: str,
        prompt: str,
        repository_summary: Optional[dict[str, Any]] = None,
        max_steps: int = 10,
    ) -> TaskPlan:
        """Create an initial dependency-aware execution plan."""

        normalized = prompt.strip()
        base_steps = self._default_steps(normalized)
        scoped_steps = self._repository_aware_steps(repository_summary or {})

        merged = self._dedupe_steps(base_steps + scoped_steps)
        ordered = merged[: max_steps if max_steps > 0 else 1]

        task_steps: list[TaskStep] = []
        previous_id: Optional[str] = None
        for index, step in enumerate(ordered, start=1):
            step_id = f"{task_id}-step-{index}"
            task_steps.append(
                TaskStep(
                    id=step_id,
                    description=step["description"],
                    agent_role=step["agent_role"],
                    status=TaskStatus.PENDING,
                    dependencies=[previous_id] if previous_id else [],
                    metadata=step.get("metadata", {}),
                )
            )
            previous_id = step_id

        return TaskPlan(
            task_id=task_id,
            steps=task_steps,
            metadata={
                "strategy": "adaptive_hierarchical",
                "prompt_fingerprint": self._fingerprint(normalized),
                "repository_signals": repository_summary or {},
            },
        )

    def replan(
        self,
        plan: TaskPlan,
        feedback: list[str],
        max_additional_steps: int = 3,
    ) -> TaskPlan:
        """Inject corrective steps after validation or execution failures."""

        if not feedback:
            return plan

        updated_steps = list(plan.steps)
        previous_id = updated_steps[-1].id if updated_steps else None
        base_index = len(updated_steps)

        for offset, feedback_item in enumerate(feedback[: max_additional_steps], start=1):
            new_step_id = f"{plan.task_id}-step-{base_index + offset}"
            updated_steps.append(
                TaskStep(
                    id=new_step_id,
                    description=f"Address validation issue: {feedback_item}",
                    agent_role="debugger",
                    status=TaskStatus.PENDING,
                    dependencies=[previous_id] if previous_id else [],
                    metadata={"replan_reason": feedback_item},
                )
            )
            previous_id = new_step_id

        metadata = dict(plan.metadata)
        metadata["replan_count"] = int(metadata.get("replan_count", 0)) + 1
        metadata["latest_feedback"] = feedback[:max_additional_steps]

        return TaskPlan(task_id=plan.task_id, steps=updated_steps, metadata=metadata)

    def _default_steps(self, prompt: str) -> list[dict[str, Any]]:
        keywords = prompt.lower()

        steps: list[dict[str, Any]] = [
            {
                "description": "Reconstruct intent and collect active workspace context",
                "agent_role": "planner",
            },
            {
                "description": "Retrieve repository and memory evidence relevant to the task",
                "agent_role": "retriever",
            },
            {
                "description": "Formulate solution strategy and risk boundaries",
                "agent_role": "reasoner",
            },
        ]

        if any(token in keywords for token in ["fix", "bug", "error", "failure"]):
            steps.append(
                {
                    "description": "Implement a focused code patch for the suspected fault",
                    "agent_role": "coder",
                }
            )
            steps.append(
                {
                    "description": "Run validation checks and debugging probes",
                    "agent_role": "debugger",
                }
            )
        else:
            steps.append(
                {
                    "description": "Implement and stage task-aligned code changes",
                    "agent_role": "coder",
                }
            )

        steps.extend(
            [
                {
                    "description": "Review implementation quality, edge cases, and safety",
                    "agent_role": "reviewer",
                },
                {
                    "description": "Persist useful execution signals into memory",
                    "agent_role": "memory",
                },
            ]
        )

        return steps

    def _repository_aware_steps(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        files = int(summary.get("files", 0) or 0)
        symbols = int(summary.get("symbols", 0) or 0)

        steps: list[dict[str, Any]] = []
        if files > 500:
            steps.append(
                {
                    "description": "Partition execution by subsystem boundaries before code edits",
                    "agent_role": "planner",
                    "metadata": {"reason": "large_repository"},
                }
            )

        if symbols > 1500:
            steps.append(
                {
                    "description": "Prioritize symbol-level retrieval to reduce context drift",
                    "agent_role": "retriever",
                    "metadata": {"reason": "high_symbol_density"},
                }
            )

        return steps

    def _dedupe_steps(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for step in steps:
            key = re.sub(r"\s+", " ", step["description"].strip().lower())
            if key in seen:
                continue
            deduped.append(step)
            seen.add(key)
        return deduped

    def _fingerprint(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
