"""Execution validation and reliability checks for orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from velune.orchestration.schemas import OrchestrationState
from velune.cognition.verification import ReasoningVerifier


class ExecutionValidator:
    """Validates state quality before allowing orchestration to finalize."""

    def __init__(self, reasoning_verifier: Optional[ReasoningVerifier] = None) -> None:
        self.verifier = reasoning_verifier or ReasoningVerifier()

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

        # Wire ReasoningVerifier patch-auditing
        proposed_patches = state.execution_state.get("proposed_patches", [])
        for patch in proposed_patches:
            file_path = patch.get("file_path")
            proposed_code = patch.get("proposed_code", "")
            existing_code = patch.get("existing_code", "")

            if file_path:
                full_path = file_path
                if not Path(full_path).is_absolute():
                    full_path = str(workspace_path / file_path)

                audit = self.verifier.audit_patch(
                    file_path=full_path,
                    proposed_code=proposed_code,
                    existing_code=existing_code,
                )
                if not audit["passed"]:
                    for issue in audit["issues"]:
                        issues.append(f"patch_contradiction: {file_path} - {issue}")

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
        # Mark contradiction issues as retryable so the agent gets a chance to replan and fix AST errors!
        for issue in issues:
            if issue.startswith("patch_contradiction:"):
                return True

        return any(issue in retryable for issue in issues)
