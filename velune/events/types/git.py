"""Git event types."""

from dataclasses import dataclass
from typing import Any


@dataclass
class CommitCreated:
    """Event emitted when a commit is created."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    commit_hash: str
    author: str
    message: str
    branch: str


@dataclass
class BranchChanged:
    """Event emitted when the branch is changed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    old_branch: str
    new_branch: str


@dataclass
class StashPushed:
    """Event emitted when a stash is pushed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    stash_ref: str
