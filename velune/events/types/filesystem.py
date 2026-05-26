"""Filesystem event types."""

from dataclasses import dataclass
from typing import Any


@dataclass
class FileCreated:
    """Event emitted when a file is created."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    file_path: str
    file_size: int


@dataclass
class FileModified:
    """Event emitted when a file is modified."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    file_path: str
    file_size: int


@dataclass
class FileDeleted:
    """Event emitted when a file is deleted."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    file_path: str
