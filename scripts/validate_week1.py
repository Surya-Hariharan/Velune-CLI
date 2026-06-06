#!/usr/bin/env python3
"""Week 1 validation — run after all prompts are complete."""
import subprocess
import sys

CHECKS = [
    (
        "python -m pytest tests/ --tb=short -q",
        "All tests pass",
    ),
    (
        "python -c \"from velune.hardware.detector import HardwareDetector; "
        "p = HardwareDetector().detect(); print('Hardware tier:', p.tier.value)\"",
        "Hardware detection works",
    ),
    (
        "python -c \"from velune.providers.keystore import list_configured_providers; "
        "print('Configured:', list_configured_providers())\"",
        "Keystore loads",
    ),
    (
        "python -c \"from velune.providers.adapters.groq import GROQ_MODELS; "
        "print('Groq models:', len(GROQ_MODELS))\"",
        "Groq provider loads",
    ),
    (
        "python -c \"from velune.execution.diff_preview import DiffPreview; "
        "print('DiffPreview OK')\"",
        "Diff preview loads",
    ),
    (
        "python -c \"from velune.telemetry.token_tracker import SessionUsage; "
        "print('TokenTracker OK')\"",
        "Token tracker loads",
    ),
    (
        "python -c \"from velune.execution.cancellation import InferenceGuard; "
        "print('InferenceGuard OK')\"",
        "Cancellation guard loads",
    ),
    (
        "python -c \"from velune.cli.commands.setup import PROVIDER_METADATA; "
        "print('Providers:', list(PROVIDER_METADATA.keys()))\"",
        "Setup wizard loads",
    ),
    (
        # Use isolation so hatchling is fetched automatically if not installed
        "python -m build --wheel -q",
        "Package builds",
    ),
]

all_pass = True
for cmd, label in CHECKS:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    status = "PASS" if result.returncode == 0 else "FAIL"
    if status == "FAIL":
        all_pass = False
        print(f"[{status}] {label}")
        print(f"       {(result.stderr or result.stdout).strip()[:120]}")
    else:
        print(f"[{status}] {label}")

print()
if all_pass:
    print("Week 1 complete — ready for Week 2 (REPL Core).")
else:
    print("Fix failing checks before proceeding.")
    sys.exit(1)
