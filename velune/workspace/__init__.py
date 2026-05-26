"""Workspace cognition."""

from velune.workspace.awareness.environment import EnvironmentAwareness
from velune.workspace.awareness.git import GitAwareness
from velune.workspace.awareness.terminal import TerminalAwareness
from velune.workspace.cognition.model import LiveCognitionModel
from velune.workspace.cognition.queries import CognitionModelQueries
from velune.workspace.cognition.updater import CognitionModelUpdater
from velune.workspace.state.machine import WorkspaceStateMachine

__all__ = [
    "WorkspaceStateMachine",
    "LiveCognitionModel",
    "CognitionModelUpdater",
    "CognitionModelQueries",
    "GitAwareness",
    "TerminalAwareness",
    "EnvironmentAwareness",
]
