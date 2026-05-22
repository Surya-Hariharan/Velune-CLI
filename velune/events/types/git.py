"""Git event types."""

from dataclasses import dataclass
from typing import Dict, Any
from velune.events.bus.engine import Event


@dataclass
class CommitCreated(Event):
    """Event emitted when a commit is created."""
    commit_hash: str
    author: str
    message: str
    branch: str


@dataclass
class BranchChanged(Event):
    """Event emitted when the branch is changed."""
    old_branch: str
    new_branch: str


@dataclass
class StashPushed(Event):
    """Event emitted when a stash is pushed."""
    stash_ref: str
