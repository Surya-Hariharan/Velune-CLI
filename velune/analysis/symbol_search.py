"""Smart symbol search: resolves @@term to indexed symbols via the SymbolRegistry.

SymbolSearcher queries the SQLite registry with a LIKE pattern, then re-ranks
the raw results using the same fuzzy_score() used for slash-command completion
so the most relevant symbol appears first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.ast_parser import Symbol
    from velune.repository.symbol_registry import SymbolRegistry

MAX_RESULTS = 8


class SymbolSearcher:
    """Search the indexed symbol registry by a fuzzy term."""

    async def search(
        self,
        term: str,
        registry: SymbolRegistry,
        max_results: int = MAX_RESULTS,
    ) -> list[Symbol]:
        """Return symbols whose names contain *term*, ranked by fuzzy score."""
        from velune.cli.autocomplete import fuzzy_score

        raw = await registry.search_symbols(f"%{term}%")
        if not raw:
            return []

        # Re-rank by fuzzy score so exact/prefix matches float to the top
        scored = [(fuzzy_score(term, s.name), s) for s in raw]
        scored = [(sc, s) for sc, s in scored if sc > 0]
        scored.sort(key=lambda t: (-t[0], t[1].name))
        return [s for _, s in scored[:max_results]]

    def format_as_context(self, symbols: list[Symbol]) -> str:
        """Format matched symbols as a context block for LLM injection."""
        if not symbols:
            return ""

        blocks: list[str] = []
        for s in symbols:
            # Build a compact signature line
            params = ", ".join(s.parameters) if s.parameters else ""
            ret = f" -> {s.return_type}" if s.return_type else ""
            signature = f"{s.kind.value} {s.name}({params}){ret}"
            location = f"{s.file_path}:{s.line_start}"
            doc = f"\n  {s.docstring.splitlines()[0]}" if s.docstring else ""

            blocks.append(
                f"[SYMBOL: {s.name}]  {location}\n  {signature}{doc}\n[END SYMBOL: {s.name}]"
            )

        return "\n\n".join(blocks)
