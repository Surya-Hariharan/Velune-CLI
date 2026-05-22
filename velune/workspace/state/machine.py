"""Workspace state machine."""

from enum import Enum
from typing import Optional, Callable
from velune.core.types import WorkspaceState


class WorkspaceStateMachine:
    """State machine for workspace state transitions."""

    def __init__(self):
        self.current_state = WorkspaceState.IDLE
        self._transition_handlers: dict[tuple[WorkspaceState, WorkspaceState], list[Callable]] = {}

    def get_state(self) -> WorkspaceState:
        """Get current state."""
        return self.current_state

    def transition_to(self, new_state: WorkspaceState) -> bool:
        """Transition to a new state."""
        if not self._can_transition(self.current_state, new_state):
            return False
        
        old_state = self.current_state
        self.current_state = new_state
        
        # Call transition handlers
        for handler in self._transition_handlers.get((old_state, new_state), []):
            handler(old_state, new_state)
        
        return True

    def _can_transition(self, from_state: WorkspaceState, to_state: WorkspaceState) -> bool:
        """Check if transition is valid."""
        # Define valid transitions
        valid_transitions = {
            WorkspaceState.IDLE: [WorkspaceState.TASK_ACTIVE, WorkspaceState.INDEXING],
            WorkspaceState.TASK_ACTIVE: [WorkspaceState.IDLE, WorkspaceState.DEBUGGING, WorkspaceState.REVIEWING],
            WorkspaceState.DEBUGGING: [WorkspaceState.TASK_ACTIVE, WorkspaceState.IDLE],
            WorkspaceState.REVIEWING: [WorkspaceState.TASK_ACTIVE, WorkspaceState.IDLE],
            WorkspaceState.INDEXING: [WorkspaceState.IDLE],
            WorkspaceState.ERROR: [WorkspaceState.IDLE],
        }
        
        return to_state in valid_transitions.get(from_state, [])

    def on_transition(self, from_state: WorkspaceState, to_state: WorkspaceState, handler: Callable) -> None:
        """Register a handler for a state transition."""
        key = (from_state, to_state)
        if key not in self._transition_handlers:
            self._transition_handlers[key] = []
        self._transition_handlers[key].append(handler)
