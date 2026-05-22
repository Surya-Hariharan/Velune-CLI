"""Execution validation and reliability checks for orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from velune.orchestration.schemas import OrchestrationState


class ExecutionValidator:
    """Validates state quality before allowing orchestration to finalize."""

    def validate(self, state: OrchestrationState) -> list[str]:
        issues: list[str] = []

        if not state.task_plan or not state.task_plan.steps:
            issues.append("missing_task_plan")

        if not state.retrieval_result or not state.retrieval_result.hits:
            issues.append("insufficient_retrieval_evidence")

        if not state.repository_snapshot:
            issues.append("missing_repository_snapshot")

        workspace_path = Path(state.request.workspace)
        if not workspace_path.exists():
            issues.append("workspace_not_found")

        if state.output and "TODO" in state.output:
            issues.append("incomplete_reasoning_output")

        return issues

    def should_retry(self, issues: list[str], attempt: int, max_retries: int) -> bool:
        """Gate autonomous retry loops based on issue severity and budget."""

        if not issues:
            return False
        if attempt >= max_retries + 1:
            return False

        retryable = {
            "insufficient_retrieval_evidence",
            "incomplete_reasoning_output",
            "missing_task_plan",
        }
        return any(issue in retryable for issue in issues)
