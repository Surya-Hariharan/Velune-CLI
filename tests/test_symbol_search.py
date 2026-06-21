"""Tests for SymbolSearcher, parse_symbol_mentions, and @@ autocomplete."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from velune.analysis.symbol_search import SymbolSearcher
from velune.cli.autocomplete import SlashCompleter
from velune.context.mentions import parse_symbol_mentions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_symbol(name: str, kind: str = "function", file_path: str = "app.py", line: int = 1):
    """Build a minimal Symbol-like object (no DB required)."""
    from velune.repository.ast_parser import Symbol, SymbolKind

    return Symbol(
        id=f"sym-{name}",
        name=name,
        kind=SymbolKind(kind),
        file_path=file_path,
        line_start=line,
        line_end=line + 5,
        docstring=f"Docstring for {name}",
        parameters=["token"],
        return_type="bool",
        is_exported=True,
    )


def _make_registry(*names: str):
    """Return an async mock registry whose search_symbols returns matching symbols."""
    symbols = [_make_symbol(n) for n in names]
    registry = MagicMock()

    async def _search(pattern: str) -> list:
        term = pattern.strip("%")
        return [s for s in symbols if term.lower() in s.name.lower()]

    registry.search_symbols = _search
    return registry


def _completions(completer: SlashCompleter, text: str) -> list[str]:
    from prompt_toolkit.document import Document

    return [c.text for c in completer.get_completions(Document(text), None)]


# ---------------------------------------------------------------------------
# SymbolSearcher
# ---------------------------------------------------------------------------

class TestSymbolSearcher:
    async def test_fuzzy_match_returns_relevant_symbols(self):
        registry = _make_registry("jwt_encode", "jwt_decode", "unrelated_func")
        results = await SymbolSearcher().search("jwt", registry)
        names = [s.name for s in results]
        assert "jwt_encode" in names
        assert "jwt_decode" in names
        assert "unrelated_func" not in names

    async def test_empty_registry_returns_no_results(self):
        registry = _make_registry()
        results = await SymbolSearcher().search("jwt", registry)
        assert results == []

    async def test_max_results_capped(self):
        names = [f"func_{i}" for i in range(20)]
        registry = _make_registry(*names)
        results = await SymbolSearcher().search("func", registry, max_results=5)
        assert len(results) <= 5

    async def test_exact_match_ranked_first(self):
        registry = _make_registry("parse_jwt", "jwt", "jwt_helper")
        results = await SymbolSearcher().search("jwt", registry)
        assert results[0].name == "jwt"

    def test_format_as_context_includes_name(self):
        searcher = SymbolSearcher()
        syms = [_make_symbol("verify_token")]
        ctx = searcher.format_as_context(syms)
        assert "verify_token" in ctx
        assert "[SYMBOL:" in ctx
        assert "[END SYMBOL:" in ctx

    def test_format_empty_list_returns_empty_string(self):
        assert SymbolSearcher().format_as_context([]) == ""


# ---------------------------------------------------------------------------
# parse_symbol_mentions
# ---------------------------------------------------------------------------

class TestSymbolMentionParsing:
    async def test_double_at_extracted_from_prompt(self):
        registry = _make_registry("jwt_encode")
        text = "explain @@jwt_encode please"
        cleaned, ctx, unresolved = await parse_symbol_mentions(text, registry)
        assert "@@jwt_encode" not in cleaned
        assert "jwt_encode" in ctx
        assert unresolved == []

    async def test_single_at_untouched_by_symbol_parser(self):
        registry = _make_registry("auth.py")
        text = "explain @auth.py"
        cleaned, ctx, unresolved = await parse_symbol_mentions(text, registry)
        # single-@ tokens are not matched by the symbol regex
        assert "@auth.py" in cleaned

    async def test_unresolved_term_returned_in_list(self):
        registry = _make_registry()  # empty
        text = "what does @@missing_func do"
        cleaned, ctx, unresolved = await parse_symbol_mentions(text, registry)
        assert "missing_func" in unresolved
        assert ctx == ""

    async def test_no_double_at_returns_unchanged(self):
        registry = _make_registry("foo")
        text = "just a plain prompt"
        cleaned, ctx, unresolved = await parse_symbol_mentions(text, registry)
        assert cleaned == text
        assert ctx == ""
        assert unresolved == []

    async def test_multiple_mentions_resolved(self):
        registry = _make_registry("encode", "decode")
        text = "compare @@encode and @@decode"
        cleaned, ctx, unresolved = await parse_symbol_mentions(text, registry)
        assert unresolved == []
        assert "encode" in ctx
        assert "decode" in ctx


# ---------------------------------------------------------------------------
# @@ autocomplete
# ---------------------------------------------------------------------------

class TestSymbolCompleter:
    def test_double_at_triggers_symbol_completion(self):
        completer = SlashCompleter(
            symbol_names=["jwt_encode", "jwt_decode", "parse_token"]
        )
        results = _completions(completer, "explain @@jwt")
        assert "jwt_encode" in results or "jwt_decode" in results

    def test_symbol_completion_uses_fuzzy_score(self):
        completer = SlashCompleter(symbol_names=["jwt_encode", "unrelated"])
        results = _completions(completer, "@@jwt")
        assert "jwt_encode" in results
        assert "unrelated" not in results

    def test_empty_symbol_cache_no_completions(self):
        completer = SlashCompleter(symbol_names=[])
        results = _completions(completer, "@@anything")
        assert results == []

    def test_slash_command_still_completes(self):
        from velune.cli.autocomplete import CommandEntry

        entries = [CommandEntry(name="lint", description="Lint", category="Code")]
        completer = SlashCompleter(commands=entries, symbol_names=["foo"])
        results = _completions(completer, "/li")
        assert "lint" in results

    def test_set_symbol_names_updates_cache(self):
        completer = SlashCompleter(symbol_names=[])
        completer.set_symbol_names(["new_func"])
        results = _completions(completer, "@@new")
        assert "new_func" in results
