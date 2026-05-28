"""Adaptive planning and replanning service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from velune.core.types.task import TaskPlan, TaskStatus, TaskStep
from velune.core.types.inference import InferenceRequest

logger = logging.getLogger("velune.planning.service")

PLANNING_TIMEOUT = 60.0

PLANNING_PROMPT_TEMPLATE = """You are a software engineering task planner.

TASK: {prompt}
{context}

Create a concrete execution plan with {max_steps} or fewer steps.

Each step must be actionable by one of these agents:
- planner: gather context, analyze requirements
- retriever: search codebase for relevant files
- coder: write or modify code files  
- debugger: diagnose and fix errors
- reviewer: validate correctness and quality
- memory: record findings and results

Respond with ONLY a JSON array:
[
  {{"id": "step-1", "description": "specific action", "agent_role": "coder", "dependencies": []}},
  {{"id": "step-2", "description": "specific action", "agent_role": "reviewer", "dependencies": ["step-1"]}}
]

IMPORTANT: Use simple stable IDs like 'step-1', 'step-2', etc.
Dependencies MUST reference IDs of other steps in this exact list.

Be specific. Mention actual file names if they can be inferred. No placeholders."""


class AdaptivePlanningService:
    """Builds and revises hierarchical engineering execution plans."""

    def create_plan(
        self,
        task_id: str,
        prompt: str,
        repository_summary: dict[str, Any] | None = None,
        max_steps: int = 10,
    ) -> TaskPlan:
        """Create an initial dependency-aware execution plan."""
        normalized = prompt.strip()
        base_steps = self._default_steps(normalized)
        ordered = base_steps[: max_steps if max_steps > 0 else 1]

        task_steps: list[TaskStep] = []
        previous_id: str | None = None
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

    async def create_plan_with_llm(
        self,
        task_id: str,
        prompt: str,
        provider,
        model_id: str,
        repository_summary: dict | None = None,
        max_steps: int = 10,
    ) -> TaskPlan:
        """Create a plan using LLM for intelligent task decomposition.
        
        Falls back to keyword-based planning if LLM call fails.
        """
        context = ""
        if repository_summary:
            files = repository_summary.get("total_files", 0) or repository_summary.get("files", 0)
            symbols = repository_summary.get("total_symbols", 0) or repository_summary.get("symbols", 0)
            langs = repository_summary.get("languages", {})
            context = f"Repository: {files} files, {symbols} symbols. Languages: {langs}"
        
        context_str = f"CODEBASE CONTEXT: {context}" if context else ""
        planning_prompt = PLANNING_PROMPT_TEMPLATE.format(
            prompt=prompt,
            context=context_str,
            max_steps=max_steps,
        )
        
        try:
            request = InferenceRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": planning_prompt}],
                temperature=0.2,
                max_tokens=1000,
            )
            response = await asyncio.wait_for(provider.infer(request), timeout=PLANNING_TIMEOUT)
            
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.split("```")[0].strip()
            
            steps_data = json.loads(content)
            
            # Build mapping from LLM-generated ID to prefixed ID
            id_mapping: dict[str, str] = {}
            for step_data in steps_data[:max_steps]:
                original_id = step_data.get("id", f"step-{len(id_mapping)+1}")
                prefixed_id = f"{task_id}-{original_id}"
                id_mapping[original_id] = prefixed_id
            
            logger.debug("LLM plan ID mapping constructed: %s", id_mapping)
            
            task_steps = []
            for step_data in steps_data[:max_steps]:
                original_id = step_data.get("id", f"step-{len(task_steps)+1}")
                prefixed_id = id_mapping[original_id]
                
                # Remap dependencies using the mapping table
                raw_deps = step_data.get("dependencies", [])
                remapped_deps = [id_mapping[d] for d in raw_deps if d in id_mapping]
                
                unmapped = [d for d in raw_deps if d not in id_mapping]
                if unmapped:
                    logger.warning(
                        "LLM plan step '%s' references unknown deps: %s",
                        original_id, unmapped
                    )
                
                task_steps.append(TaskStep(
                    id=prefixed_id,
                    description=step_data.get("description", ""),
                    agent_role=step_data.get("agent_role", "coder"),
                    status=TaskStatus.PENDING,
                    dependencies=remapped_deps,  # Now correctly prefixed
                    metadata={},
                ))
            
            total_edges = sum(len(step.dependencies) for step in task_steps)
            logger.info("LLM plan created: %d steps, %d dependency edges", len(task_steps), total_edges)
            
            return TaskPlan(
                task_id=task_id,
                steps=task_steps,
                metadata={
                    "strategy": "llm_decomposed",
                    "prompt_fingerprint": self._fingerprint(prompt),
                    "repository_signals": repository_summary or {},
                },
            )
        
        except Exception as e:
            logger.warning(
                "LLM planning failed (%s), falling back to keyword-based planning: %s",
                type(e).__name__, e
            )
            return self.create_plan(
                task_id=task_id,
                prompt=prompt,
                repository_summary=repository_summary,
                max_steps=max_steps,
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
        return [
            {"description": "Gather project requirements and stage constraints.", "agent_role": "planner"},
            {"description": "Retrieve repository and memory evidence relevant to the task.", "agent_role": "retriever"},
            {"description": "Implement and stage task-aligned code changes.", "agent_role": "coder"},
            {"description": "Review implementation quality, edge cases, and safety.", "agent_role": "reviewer"},
            {"description": "Persist useful execution signals into memory.", "agent_role": "memory"},
        ]

    def _fingerprint(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
