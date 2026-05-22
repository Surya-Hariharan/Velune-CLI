"""Filesystem event types."""

from dataclasses import dataclass
from typing import Dict, Any
from velune.events.bus.engine import Event


@dataclass
class FileCreated(Event):
    """Event emitted when a file is created."""
    file_path: str
    file_size: int


@dataclass
class FileModified(Event):
    """Event emitted when a file is modified."""
    file_path: str
    file_size: int


@dataclass
class FileDeleted(Event):
    """Event emitted when a file is deleted."""
    file_path: str
