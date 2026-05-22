"""Orchestration-related errors."""


class OrchestrationError(Exception):
    """Base exception for orchestration errors."""
    pass


class AgentExecutionError(OrchestrationError):
    """Raised when agent execution fails."""
    pass


class PipelineExecutionError(OrchestrationError):
    """Raised when pipeline execution fails."""
    pass


class StateTransitionError(OrchestrationError):
    """Raised when state transition fails."""
    pass


class CheckpointError(OrchestrationError):
    """Raised when checkpoint operation fails."""
    pass
