"""Pattern learning from task completions."""

from typing import Dict, Any
from velune.memory.procedural.store import ProceduralMemoryStore
from velune.core.types import MemoryRecord, MemoryType, Task, TaskResult


class ProceduralLearner:
    """Learns procedural patterns from task completions."""

    def __init__(self, store: ProceduralMemoryStore):
        self.store = store

    def learn_from_task(
        self,
        task: Task,
        result: TaskResult,
    ) -> Optional[MemoryRecord]:
        """Learn a procedural pattern from a completed task."""
        if not result.success:
            return None
        
        # Extract pattern from task and result
        pattern_content = self._extract_pattern(task, result)
        
        if not pattern_content:
            return None
        
        import uuid
        from datetime import datetime
        
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.PROCEDURAL,
            content=pattern_content,
            importance=self._calculate_importance(task, result),
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.now(),
            expires_at=None,
            metadata={
                "task_id": task.id,
                "task_description": task.description,
                "execution_time_ms": result.execution_time_ms,
            },
        )
        
        self.store.add(record)
        return record

    def _extract_pattern(self, task: Task, result: TaskResult) -> str:
        """Extract a pattern from task and result."""
        pattern = f"Task: {task.description}\n"
        
        if result.output:
            pattern += f"Solution: {result.output}\n"
        
        pattern += f"Execution time: {result.execution_time_ms}ms\n"
        
        return pattern

    def _calculate_importance(self, task: Task, result: TaskResult) -> float:
        """Calculate importance of a procedural pattern."""
        # Higher importance for:
        # - High priority tasks
        # - Fast execution
        # - Complex tasks (many steps)
        
        importance = 0.5
        
        # Priority factor
        importance += (task.priority / 10) * 0.2
        
        # Speed factor (faster is better)
        if result.execution_time_ms < 10000:  # < 10 seconds
            importance += 0.2
        elif result.execution_time_ms < 60000:  # < 1 minute
            importance += 0.1
        
        # Complexity factor
        if result.steps_total > 5:
            importance += 0.1
        
        return min(importance, 1.0)
