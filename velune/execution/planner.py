"""Translates Council plans into Directed Acyclic Graphs (DAG) of dependent commands."""

from __future__ import annotations

import logging

from velune.core.errors.execution import ExecutionError
from velune.core.types.task import TaskPlan, TaskStep

logger = logging.getLogger("velune.execution.planner")


class ExecutionDAG:
    """A Directed Acyclic Graph representing dependent step tasks."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.steps: dict[str, TaskStep] = {}
        self.adj_list: dict[str, set[str]] = {}  # step_id -> dependent step_ids
        self.in_degree: dict[str, int] = {}  # step_id -> number of prerequisites

    def add_step(self, step: TaskStep) -> None:
        """Add a TaskStep and establish dependency connections."""
        self.steps[step.id] = step
        if step.id not in self.adj_list:
            self.adj_list[step.id] = set()
        if step.id not in self.in_degree:
            self.in_degree[step.id] = 0

        for dep in step.dependencies:
            # Set up dependency connection (dep -> step.id)
            if dep not in self.adj_list:
                self.adj_list[dep] = set()

            if step.id not in self.adj_list[dep]:
                self.adj_list[dep].add(step.id)
                self.in_degree[step.id] = self.in_degree.get(step.id, 0) + 1

    def topological_sort(self) -> list[TaskStep]:
        """Compute the topological sort using Kahn's Algorithm.

        Ensures dependency orders are strictly adhered to. Detects circular graphs.
        """
        in_degrees = self.in_degree.copy()

        # Ensure all steps added are represented in in_degrees
        for step_id in self.steps:
            if step_id not in in_degrees:
                in_degrees[step_id] = 0

        # Queue nodes with no incoming dependencies (in_degree = 0)
        queue = [sid for sid, degree in in_degrees.items() if degree == 0]
        sorted_steps: list[TaskStep] = []

        while queue:
            # Sort to preserve list order where possible
            queue.sort()
            current_id = queue.pop(0)
            sorted_steps.append(self.steps[current_id])

            # Decrease incoming degree of all dependents
            for dependent_id in self.adj_list.get(current_id, set()):
                if dependent_id in in_degrees:
                    in_degrees[dependent_id] -= 1
                    if in_degrees[dependent_id] == 0:
                        queue.append(dependent_id)

        # If sorted steps is not matching total steps, there is a circular dependency!
        if len(sorted_steps) != len(self.steps):
            circular_candidates = [sid for sid, deg in in_degrees.items() if deg > 0]
            raise ExecutionError(
                f"Circular dependency detected in plan. Unresolvable steps: {circular_candidates}"
            )

        return sorted_steps


class ExecutionPlanner:
    """Translates high-level council TaskPlans into DAG Execution chains."""

    def compile(self, plan: TaskPlan) -> ExecutionDAG:
        """Compile a standard TaskPlan into a verifiable ExecutionDAG."""
        logger.info("Compiling plan %s containing %d steps", plan.task_id, len(plan.steps))
        dag = ExecutionDAG(plan.task_id)

        for step in plan.steps:
            dag.add_step(step)

        # Validate by dry-running topological sort
        dag.topological_sort()
        return dag
