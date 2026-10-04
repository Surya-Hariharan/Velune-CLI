"""Private architecture docs (docs/internal/) must only reference code that exists.

docs/internal/ is git-ignored, so this test runs on a developer machine that has the folder and skips
everywhere else (CI, fresh clones). It catches the most common drift: a module or file is renamed or
deleted and the architecture notes keep pointing at it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = ROOT / "docs" / "internal"

pytestmark = pytest.mark.skipif(
    not INTERNAL.is_dir(), reason="docs/internal is private and absent in this checkout"
)

_PATH_TOKEN = re.compile(
    r"`(velune/[A-Za-z0-9_./-]+\.(?:py|toml|json|md|txt))(?::[A-Za-z_][\w.]*)?`"
)
_LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def _docs() -> list[Path]:
    return sorted(INTERNAL.glob("*.md"))


def test_internal_docs_exist():
    names = {p.name for p in _docs()}
    assert "README.md" in names
    assert any(n.startswith("01-") for n in names)


@pytest.mark.parametrize("doc", _docs(), ids=lambda p: p.name)
def test_referenced_source_paths_exist(doc: Path):
    text = doc.read_text(encoding="utf-8")
    missing = sorted(
        {
            path
            for path in _PATH_TOKEN.findall(text)
            if not (ROOT / path).exists()
            # Paths in docs that explicitly describe a missing/removed file are listed below.
            and path not in _KNOWN_ABSENT
        }
    )
    assert not missing, f"{doc.name} references files that do not exist: {missing}"


@pytest.mark.parametrize("doc", _docs(), ids=lambda p: p.name)
def test_cross_links_between_internal_docs_resolve(doc: Path):
    text = doc.read_text(encoding="utf-8")
    broken = [
        target
        for target in _LINK.findall(text)
        if not target.startswith(("http://", "https://")) and not (doc.parent / target).exists()
    ]
    assert not broken, f"{doc.name} has broken links: {broken}"


# Files the internal docs name on purpose while saying they do NOT exist.
_KNOWN_ABSENT = {
    "velune/cognition/prompts/_premium.example.py",
}
