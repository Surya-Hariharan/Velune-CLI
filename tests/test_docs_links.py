"""Relative links in the tracked Markdown files must point at files that exist."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def _tracked_markdown() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]


_DOCS = [p for p in _tracked_markdown() if ".claude" not in p.parts]

pytestmark = pytest.mark.skipif(not _DOCS, reason="not a git checkout")


@pytest.mark.parametrize("doc", _DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc: Path):
    text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
    broken = []
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if path and not (doc.parent / path).exists():
            broken.append(target)
    assert not broken, f"{doc.relative_to(ROOT)} has broken links: {broken}"
