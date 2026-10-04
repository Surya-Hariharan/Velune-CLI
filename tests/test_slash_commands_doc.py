"""docs/slash-commands.md must describe exactly the commands the REPL registers.

The reference tables are generated from the registry (name, aliases, usage, description), so any command
added, renamed or re-described without updating the doc fails here instead of silently drifting.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from velune.cli.slash_dispatcher import _BUILTIN_CATEGORIES, build_slash_registry

DOC = Path(__file__).resolve().parents[1] / "docs" / "slash-commands.md"

pytestmark = pytest.mark.skipif(not DOC.is_file(), reason="docs/slash-commands.md not present")


@pytest.fixture(scope="module")
def commands():
    return [c for c in build_slash_registry(MagicMock()).all_unique() if not c.hidden]


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_every_builtin_command_has_a_category(commands):
    uncategorised = [c.name for c in commands if c.name not in _BUILTIN_CATEGORIES]
    assert not uncategorised, f"add these to _BUILTIN_CATEGORIES: {uncategorised}"


def test_every_command_is_documented_with_its_current_usage_and_description(commands, doc_text):
    missing = []
    for cmd in commands:
        escaped_usage = cmd.usage.replace("|", "\\|")
        escaped_desc = cmd.description.replace("|", "\\|")
        row = re.search(rf"^\| `{re.escape(cmd.name)}` \|.*$", doc_text, re.M)
        if row is None:
            missing.append(f"/{cmd.name} (no row)")
            continue
        text = row.group(0)
        if escaped_usage not in text:
            missing.append(f"/{cmd.name} (usage out of date)")
        if escaped_desc not in text:
            missing.append(f"/{cmd.name} (description out of date)")
        for alias in cmd.aliases:
            if f"`{alias}`" not in text:
                missing.append(f"/{cmd.name} (alias {alias!r} missing)")
    assert not missing, "docs/slash-commands.md is out of date: " + "; ".join(missing)


def test_header_command_count_matches(commands, doc_text):
    match = re.search(r"(\d+) commands across (\d+) categories", doc_text)
    assert match, "the intro line with the command count is missing"
    assert int(match.group(1)) == len(commands)
    assert int(match.group(2)) == len({c.category for c in commands})
