"""Task-to-capability mapping."""

from typing import Optional
from velune.core.types import ModelCapability


class SpecializationMapper:
    """Maps tasks to required capabilities."""

    def map_task_to_capability(self, task_description: str) -> Optional[ModelCapability]:
        """Map a task description to the primary capability needed."""
        task_lower = task_description.lower()
        
        # Code generation
        if any(
            keyword in task_lower
            for keyword in ["write", "implement", "create", "generate code", "add feature"]
        ):
            return ModelCapability.CODE_GENERATION
        
        # Code analysis
        if any(
            keyword in task_lower
            for keyword in ["analyze", "explain", "understand", "review", "audit"]
        ):
            return ModelCapability.CODE_ANALYSIS
        
        # Debugging
        if any(
            keyword in task_lower
            for keyword in ["debug", "fix bug", "error", "issue", "troubleshoot"]
        ):
            return ModelCapability.DEBUGGING
        
        # Refactoring
        if any(
            keyword in task_lower
            for keyword in ["refactor", "improve", "optimize", "clean up"]
        ):
            return ModelCapability.REFACTORING
        
        # Planning
        if any(
            keyword in task_lower
            for keyword in ["plan", "design", "architecture", "strategy"]
        ):
            return ModelCapability.PLANNING
        
        # Summarization
        if any(
            keyword in task_lower
            for keyword in ["summarize", "summary", "brief", "overview"]
        ):
            return ModelCapability.SUMMARIZATION
        
        # Default to reasoning
        return ModelCapability.REASONING
