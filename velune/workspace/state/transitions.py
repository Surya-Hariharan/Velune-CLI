"""State transition definitions."""

from velune.core.types import WorkspaceState


class StateTransitions:
    """Defines valid state transitions."""

    TRANSITIONS = {
        WorkspaceState.IDLE: [
            WorkspaceState.TASK_ACTIVE,
            WorkspaceState.INDEXING,
        ],
        WorkspaceState.TASK_ACTIVE: [
            WorkspaceState.IDLE,
            WorkspaceState.DEBUGGING,
            WorkspaceState.REVIEWING,
        ],
        WorkspaceState.DEBUGGING: [
            WorkspaceState.TASK_ACTIVE,
            WorkspaceState.IDLE,
        ],
        WorkspaceState.REVIEWING: [
            WorkspaceState.TASK_ACTIVE,
            WorkspaceState.IDLE,
        ],
        WorkspaceState.INDEXING: [
            WorkspaceState.IDLE,
        ],
        WorkspaceState.ERROR: [
            WorkspaceState.IDLE,
        ],
    }

    @classmethod
    def can_transition(cls, from_state: WorkspaceState, to_state: WorkspaceState) -> bool:
        """Check if transition is valid."""
        return to_state in cls.TRANSITIONS.get(from_state, [])
