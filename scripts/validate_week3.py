#!/usr/bin/env python3
"""Week 3 final validation -- run before tagging v1.0.0."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def run_cmd(cmd: list[str] | str, label: str, shell: bool = False) -> bool:
    """Run a command and print a pass/fail indicator."""
    result = subprocess.run(cmd, capture_output=True, text=True, shell=shell, encoding="utf-8", errors="replace")
    ok = result.returncode == 0
    indicator = "+" if ok else "x"
    print(f"  {indicator} {label}")
    if not ok:
        out = (result.stdout + result.stderr).strip()
        if out:
            for line in out.splitlines()[-8:]:
                print(f"      {line}")
    return ok


def check_file(path: str, label: str, min_bytes: int = 0) -> bool:
    """Check that a file exists and optionally meets a minimum size."""
    p = ROOT / path
    if not p.exists():
        print(f"  x {label}")
        print(f"      File not found: {p}")
        return False
    if min_bytes and p.stat().st_size < min_bytes:
        print(f"  x {label}")
        print(f"      File too small ({p.stat().st_size} bytes, need {min_bytes})")
        return False
    print(f"  + {label}")
    return True


def check_import(stmt: str, label: str) -> bool:
    """Check that a Python import + optional assertion succeeds."""
    return run_cmd([sys.executable, "-c", stmt], label)


all_pass = True

print("\nWEEK 3 VALIDATION - Velune Release Readiness\n")

print("-- Feature Imports ------------------------------------------")
imports = [
    (
        "from velune.cli.modes import ModeManager, SessionMode; ModeManager()",
        "ModeManager loads",
    ),
    (
        "from velune.cli.autocomplete import SlashCompleter; SlashCompleter()",
        "SlashCompleter loads",
    ),
    (
        "from velune.cli.banner import render_startup_banner",
        "Banner module loads",
    ),
    (
        "from velune.cli.model_selector import ModeAwareModelSelector",
        "ModeAwareModelSelector loads",
    ),
]
for stmt, label in imports:
    if not check_import(stmt, label):
        all_pass = False

print("\n-- Documentation --------------------------------------------")
docs = [
    ("README.md",       "README.md exists and is substantial",  2000),
    ("WINDOWS.md",      "WINDOWS.md exists",                    0),
    ("CONTRIBUTING.md", "CONTRIBUTING.md exists",               0),
    ("docs/mcp.md",     "docs/mcp.md exists",                   0),
    ("CHANGELOG.md",    "CHANGELOG.md exists",                  0),
    ("LICENSE",         "LICENSE exists",                       0),
]
for path, label, min_b in docs:
    if not check_file(path, label, min_b):
        all_pass = False

print("\n-- Security -------------------------------------------------")
if not run_cmd([sys.executable, "scripts/security_audit.py"], "Security audit passes"):
    all_pass = False

print("\n-- Tests ----------------------------------------------------")
if not run_cmd(
    [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short", "--timeout=30"],
    "All tests pass",
):
    all_pass = False

print("\n-- Modes ----------------------------------------------------")
modes = [
    (
        "from velune.cli.modes import ModeManager, SessionMode; "
        "m = ModeManager(); "
        "m.set_mode(SessionMode.OPTIMUS); "
        "assert m.config.council_tier == 'instant', m.config.council_tier",
        "OPTIMUS mode config is correct",
    ),
    (
        "from velune.cli.modes import ModeManager, SessionMode; "
        "m = ModeManager(); "
        "m.set_mode(SessionMode.GODLY); "
        "assert m.config.use_largest_model is True",
        "GODLY mode config is correct",
    ),
]
for stmt, label in modes:
    if not check_import(stmt, label):
        all_pass = False

print("\n-- Package Build --------------------------------------------")
build_checks = [
    (
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "-q"],
        "Wheel builds",
    ),
    (
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "-q"],
        "Source dist builds",
    ),
]
for cmd, label in build_checks:
    if not run_cmd(cmd, label):
        all_pass = False

# twine check needs the actual dist files
import glob as _glob
dist_files = _glob.glob(str(ROOT / "dist" / "*"))
if dist_files:
    if not run_cmd(
        [sys.executable, "-m", "twine", "check"] + dist_files, "Twine check passes"
    ):
        all_pass = False
else:
    print("  x Twine check passes")
    print("      No dist/ files found -build step may have failed")
    all_pass = False

print("\n-- CI Configuration ------------------------------------------")
ci_files = [
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "scripts/extract_changelog.py",
    "scripts/pre_release_check.py",
]
for path in ci_files:
    if not check_file(path, f"{path} exists"):
        all_pass = False

print("\n-- Pre-release Full Check ------------------------------------")
if not run_cmd(
    [sys.executable, "scripts/pre_release_check.py"], "Pre-release checklist passes"
):
    all_pass = False

print()
if all_pass:
    print("=" * 60)
    print("  ALL CHECKS PASSED")
    print("  Velune is ready to release.")
    print()
    print("  Release steps:")
    print("  1. Update __version__ in velune/__init__.py to 1.0.0")
    print("  2. Update CHANGELOG.md: rename [Unreleased] to [1.0.0]")
    print("  3. git add -A && git commit -m 'chore: release v1.0.0'")
    print("  4. git tag v1.0.0")
    print("  5. git push origin main --tags")
    print("  6. Monitor GitHub Actions ->release workflow")
    print("  7. Verify: pip install velune==1.0.0 && velune --version")
    print("=" * 60)
    sys.exit(0)
else:
    print("=" * 60)
    print("  SOME CHECKS FAILED -fix before releasing")
    print("=" * 60)
    sys.exit(1)
