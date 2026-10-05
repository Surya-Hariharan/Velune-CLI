"""Every installer the README tells users to download must be committed.

Regression: `.gitignore` had `scripts/*.sh` (meant for local utility
scripts), which silently excluded `scripts/install.sh`. The README's
`curl …/main/scripts/install.sh | sh` would have 404'd for every macOS/Linux
user, and CI's installer job would have failed on checkout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RAW_URL = re.compile(
    r"https://raw\.githubusercontent\.com/Surya-Hariharan/Velune-CLI/main/([\w./-]+)"
)


def _readme_installer_paths() -> list[str]:
    paths = sorted(set(RAW_URL.findall((ROOT / "README.md").read_text(encoding="utf-8"))))
    assert paths, "README no longer references any raw installer URL"
    return paths


@pytest.mark.parametrize("rel", _readme_installer_paths())
def test_readme_installer_exists(rel):
    assert (ROOT / rel).is_file(), f"README links {rel}, which does not exist"


@pytest.mark.parametrize("rel", _readme_installer_paths())
def test_readme_installer_is_not_gitignored(rel):
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", rel],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    # exit 0 = ignored, 1 = not ignored
    assert ignored.returncode == 1, f"{rel} is excluded by .gitignore and would 404 on GitHub"
