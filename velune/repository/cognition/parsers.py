"""Parser adapters for repository cognition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(slots=True)
class ParserAvailability:
    """Parser runtime capabilities."""

    available: bool
    backend: str
    detail: str = ""


class TreeSitterParserAdapter:
    """Optional tree-sitter adapter boundary.

    The service will use this when grammar packages are present; otherwise it
    falls back to AST and regex heuristics.
    """

    def __init__(self) -> None:
        self._available = False
        self._backend = "tree-sitter"
        self._detail = "tree-sitter runtime not available"

        try:
            import tree_sitter  # noqa: F401
            self._available = True
            self._detail = "tree-sitter runtime available"
        except Exception as exc:  # pragma: no cover - optional dependency boundary
            self._detail = f"tree-sitter runtime unavailable: {exc}"

    @property
    def availability(self) -> ParserAvailability:
        return ParserAvailability(available=self._available, backend=self._backend, detail=self._detail)

    def parse(self, file_path: Path, content: Optional[str] = None) -> Optional[Any]:
        """Parse a file if the runtime and grammars are installed."""

        if not self._available:
            return None
        return None