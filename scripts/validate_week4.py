#!/usr/bin/env python3
"""Week 4 / v1.1.0 release validation."""
import subprocess, sys
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def run(cmd, label, must_pass=True):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = result.returncode == 0
    print(f"  {'OK' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"    {result.stdout.strip()[-200:]}")
        if result.stderr.strip():
            print(f"    {result.stderr.strip()[-100:]}")
    return ok

all_pass = True
print("\nWEEK 4 -- v1.1.0 Release Validation\n")

print("-- Provider Imports -------------------------------------------")
providers = [
    ("python -c \"from velune.providers.adapters.google import GoogleProvider, GEMINI_MODELS; print(len(GEMINI_MODELS), 'Gemini models')\"",
     "Google Gemini provider loads"),
    ("python -c \"from velune.providers.adapters.together import TogetherProvider, TOGETHER_MODELS; print(len(TOGETHER_MODELS), 'Together models')\"",
     "Together AI provider loads"),
    ("python -c \"from velune.providers.adapters.fireworks import FireworksProvider, FIREWORKS_MODELS; print(len(FIREWORKS_MODELS), 'Fireworks models')\"",
     "Fireworks AI provider loads"),
]
for cmd, label in providers:
    if not run(cmd, label): all_pass = False

print("\n-- New Systems ------------------------------------------------")
systems = [
    ("python -c \"from velune.orchestration.role_assignments import CouncilRoleMap, COUNCIL_ROLES; rm = CouncilRoleMap(); print('Roles:', COUNCIL_ROLES)\"",
     "CouncilRoleMap loads"),
    ("python -c \"from velune.repository.project_type import ProjectTypeDetector, ProjectType; print('ProjectTypes:', len(ProjectType))\"",
     "ProjectTypeDetector loads"),
    ("python -c \"from velune.providers.ollama_manager import OllamaManager, RECOMMENDED_MODELS; print('Recommended:', len(RECOMMENDED_MODELS))\"",
     "OllamaManager loads"),
    ("python -c \"from velune.cli.modes import ModeManager; m = ModeManager(); print('Modes OK')\"",
     "ModeManager loads"),
    ("python -c \"from velune.cli.autocomplete import SlashCompleter; c = SlashCompleter(); print('Completer OK')\"",
     "SlashCompleter loads"),
]
for cmd, label in systems:
    if not run(cmd, label): all_pass = False

print("\n-- Project Type Detection -------------------------------------")
pt = [
    ("python -c \""
     "import tempfile, pathlib;"
     "from velune.repository.project_type import ProjectTypeDetector, ProjectType;"
     "tmp = pathlib.Path(tempfile.mkdtemp());"
     "(tmp / 'requirements.txt').write_text('fastapi');"
     "p = ProjectTypeDetector().detect(tmp);"
     "assert p.project_type == ProjectType.PYTHON_FASTAPI;"
     "print('FastAPI detection OK')\"",
     "FastAPI detection correct"),
    ("python -c \""
     "import tempfile, pathlib, json;"
     "from velune.repository.project_type import ProjectTypeDetector, ProjectType;"
     "tmp = pathlib.Path(tempfile.mkdtemp());"
     "(tmp / 'package.json').write_text(json.dumps({'dependencies': {'next': '14'}}));"
     "p = ProjectTypeDetector().detect(tmp);"
     "assert p.project_type == ProjectType.NODE_NEXTJS;"
     "print('Next.js detection OK')\"",
     "Next.js detection correct"),
]
for cmd, label in pt:
    if not run(cmd, label): all_pass = False

print("\n-- Version Consistency ----------------------------------------")
ver = [
    ("python -c \"import velune; assert velune.__version__ == '1.1.0'; print('Version:', velune.__version__)\"",
     "Version is 1.1.0"),
    ("grep '\\[1.1.0\\]' CHANGELOG.md",
     "CHANGELOG has 1.1.0 entry"),
]
for cmd, label in ver:
    if not run(cmd, label): all_pass = False

print("\n-- Tests ------------------------------------------------------")
if not run("python -m pytest tests/ -q --tb=short --timeout=30", "All tests pass"):
    all_pass = False

print("\n-- Security ---------------------------------------------------")
if not run("python scripts/security_audit.py", "Security audit passes"):
    all_pass = False

print("\n-- Package Build ----------------------------------------------")
if not run("python -m build --wheel --no-isolation -q", "v1.1.0 wheel builds"):
    all_pass = False

print()
if all_pass:
    print("=" * 60)
    print("  v1.1.0 READY")
    print()
    print("  Release:")
    print("  1. git add -A && git commit -m 'chore: release v1.1.0'")
    print("  2. git tag v1.1.0")
    print("  3. git push origin main --tags")
    print("=" * 60)
    sys.exit(0)
else:
    print("=" * 60)
    print("  CHECKS FAILED -- fix before tagging")
    print("=" * 60)
    sys.exit(1)
