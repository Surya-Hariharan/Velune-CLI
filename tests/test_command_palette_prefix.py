"""Typing `/c` must list the commands whose name starts with "c" — nothing else.

Regression: the palette also matched aliases and search keywords by
substring/subsequence, so `/m` surfaced /roles ("assign model") and /pull
("download model"), and `/co` ranked /index first through its `/cog` alias,
returning 29 results. Name-prefix matches now win outright; aliases and
keywords only apply when no command name starts with the text.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from prompt_toolkit.document import Document

from velune.cli.autocomplete import CommandEntry, SlashCompleter
from velune.cli.command_palette import CommandPaletteModel
from velune.cli.slash_commands import SlashCommand


async def _noop(args: str) -> None:
    pass


def _cmd(name, aliases=(), terms=(), category="General"):
    return SlashCommand(
        name=name,
        aliases=list(aliases),
        description="d",
        usage=f"/{name}",
        handler=_noop,
        category=category,
        search_terms=tuple(terms),
    )


def _commands():
    return [
        _cmd("council", aliases=["c"], terms=("multi-agent",)),
        _cmd("config", aliases=["cfg"], terms=("configuration",)),
        _cmd("connect", aliases=["login"], terms=("anthropic", "claude", "credentials")),
        _cmd("index", aliases=["cog"], terms=("codebase",)),
        _cmd("doctor", terms=("check", "diagnostics")),
        _cmd("roles", terms=("assign model", "council roles")),
        _cmd("pull", terms=("download model",)),
        _cmd("model", terms=("switch model",)),
        _cmd("mode"),
    ]


def _names(query):
    return [m.command.name for m in CommandPaletteModel(_commands()).matches(query)]


def test_single_letter_lists_only_commands_starting_with_it():
    assert set(_names("c")) == {"council", "config", "connect"}


def test_two_letters_narrow_further_and_exclude_alias_only_matches():
    # /index matches "co" only through its alias "cog" and keyword "codebase".
    assert set(_names("co")) == {"council", "config", "connect"}
    assert "index" not in _names("co")


def test_keywords_do_not_leak_into_prefix_results():
    # "model" is a keyword of /roles and /pull but neither name starts with "m".
    assert set(_names("m")) == {"model", "mode"}


def test_exact_name_ranks_first():
    assert _names("mode")[0] == "mode"


def test_keywords_still_find_commands_when_no_name_matches():
    assert _names("anthropic") == ["connect"]
    assert _names("login") == ["connect"]


def test_no_match_at_all_is_empty():
    assert _names("zzz") == []


def test_query_is_case_insensitive():
    assert set(_names("CO")) == {"council", "config", "connect"}


def test_completer_dropdown_follows_the_same_rule():
    entries = [
        CommandEntry(name=c.name, description="d", aliases=tuple(c.aliases)) for c in _commands()
    ]
    completer = SlashCompleter(commands=entries)
    shown = {c.text for c in completer.get_completions(Document("/co"), MagicMock())}
    assert shown == {"council", "config", "connect"}
    # No name starts with "ncil", so fuzzy matching still applies (typo tolerance).
    fuzzy = {c.text for c in completer.get_completions(Document("/ncil"), MagicMock())}
    assert "council" in fuzzy
