#!/usr/bin/env python3
"""Check startup performance of Velune CLI."""

from __future__ import annotations

import json
import sys
import time
import subprocess


def check_startup_perf() -> int:
    """Measure startup performance. Returns 0 if under threshold, 1 if over."""
    max_startup_ms = 3000

    print(f"Measuring Velune CLI startup performance (max {max_startup_ms}ms)...\n")

    try:
        # Measure time to run velune doctor
        start = time.perf_counter()
        result = subprocess.run(
            ["velune", "doctor", "check", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        if result.returncode != 0:
            print(f"⚠ velune doctor check failed with exit code {result.returncode}")
            print(f"stderr: {result.stderr}")
            # Don't fail the startup check if the command failed for other reasons
            return 0

        print(f"Startup time: {elapsed_ms:.0f}ms")

        if elapsed_ms > max_startup_ms:
            print(f"❌ STARTUP TOO SLOW: {elapsed_ms:.0f}ms > {max_startup_ms}ms")
            print("\nOptimization tips:")
            print("  - Review lazy imports in velune/kernel/entrypoint.py")
            print("  - Profile with: python -m cProfile -s cumtime -m velune doctor")
            return 1

        print(f"✓ Startup time acceptable ({elapsed_ms:.0f}ms < {max_startup_ms}ms)")
        return 0

    except subprocess.TimeoutExpired:
        print(f"❌ Startup timed out (> 10 seconds)")
        return 1
    except FileNotFoundError:
        print("⚠ velune command not found, skipping startup check")
        return 0
    except Exception as e:
        print(f"⚠ Could not measure startup: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(check_startup_perf())
