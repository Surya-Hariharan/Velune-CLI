"""Core types for the multi-format edit system.

EditBlock is the universal representation of a proposed file change,
regardless of which format the LLM used to express it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from velune._compat import StrEnum


class EditFormat(StrEnum):
    """Supported LLM output formats for code edits."""

    SEARCH_REPLACE = "search_replace"
    WHOLE_FILE = "whole_file"
    UDIFF = "udiff"


@dataclass
class EditBlock:
    """A single proposed file change, normalised from any LLM output format."""

    file_path: str
    original: str = ""
    proposed: str = ""
    is_new_file: bool = False
    is_deletion: bool = False
    format_used: EditFormat = EditFormat.SEARCH_REPLACE
    confidence: float = 1.0


class ParseError(Exception):
    """Raised when an edit format cannot parse the LLM response."""


class BaseEditFormat(ABC):
    """Abstract base for all edit format parsers."""

    @abstractmethod
    def parse(self, response: str, workspace_path: Path | None = None) -> list[EditBlock]:
        """Extract EditBlocks from a raw LLM response string."""

    @abstractmethod
    def format_instructions(self) -> str:
        """Return the system-prompt fragment that tells the LLM which format to use."""
