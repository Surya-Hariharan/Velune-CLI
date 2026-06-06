#!/usr/bin/env python3
"""Extract release notes for a specific version from CHANGELOG.md."""
import re
import sys
from pathlib import Path


def extract(version: str) -> str:
    changelog = Path("CHANGELOG.md")
    if not changelog.exists():
        return f"Release {version}"

    text = changelog.read_text()
    pattern = rf"## \[{re.escape(version)}\].*?\n(.*?)(?=\n## \[|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return f"Release {version} — see CHANGELOG.md for details."


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    print(extract(version))
