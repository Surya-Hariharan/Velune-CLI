#!/usr/bin/env python3
"""Benchmark: Rust scan_directory vs Python os.walk baseline.

Run with:
    python scripts/benchmark_scan.py

Or in CI after the Rust wheel is installed:
    python scripts/benchmark_scan.py --ci

Decision rule:
    Rust scan_directory is justified only if it provides >= 30% throughput
    improvement on a tree with >= 5000 files.  Below that threshold the
    Python os.walk fallback is the correct choice — it has zero build cost.

    The threshold is higher than sha256 (30% vs 20%) because replacing
    FilesystemScanner would also require porting gitignore-spec matching
    to Rust, which significantly increases the maintenance surface.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import timeit
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from velune.repository._native import (  # noqa: E402
    NATIVE_AVAILABLE,
    _scan_directory_py,
    scan_directory,
)

THRESHOLD_SPEEDUP = 1.30
ROUNDS = 10
EXTENSIONS = [".py", ".rs", ".go", ".ts", ".js", ".md"]
SKIP_NAMES = [".venv", "node_modules", "__pycache__", ".git"]


def make_tree(root: Path, n_files: int) -> None:
    """Create a flat-ish tree with n_files source files across several subdirs."""
    dirs = ["src", "tests", "docs", "scripts", "lib/core", "lib/utils", "lib/api"]
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    # Add noise directories that should be skipped
    for skip in SKIP_NAMES:
        noise = root / skip
        noise.mkdir(exist_ok=True)
        (noise / "noise.py").write_text("# noise")

    per_dir = n_files // len(dirs)
    created = 0
    for d in dirs:
        for i in range(per_dir):
            ext = EXTENSIONS[created % len(EXTENSIONS)]
            (root / d / f"file_{created}{ext}").write_text(f"# file {created}")
            created += 1
    # Remainder
    while created < n_files:
        ext = EXTENSIONS[created % len(EXTENSIONS)]
        (root / f"root_{created}{ext}").write_text(f"# file {created}")
        created += 1


def bench(label: str, fn, root: str, rounds: int) -> tuple[float, int]:
    result = [None]
    def run():
        result[0] = fn(root, EXTENSIONS, SKIP_NAMES)
    elapsed = timeit.timeit(run, number=rounds)
    count = len(result[0]) if result[0] else 0
    ms = elapsed / rounds * 1000
    print(f"  {label:<30} {ms:>8.1f} ms/call  ({count} files found)")
    return ms, count


def run_benchmarks(ci_mode: bool) -> dict:
    tree_sizes = {
        "500 files": 500,
        "2000 files": 2000,
        "5000 files": 5000,
        "10000 files": 10000,
    }

    results: dict = {
        "native_available": NATIVE_AVAILABLE,
        "threshold_speedup": THRESHOLD_SPEEDUP,
        "sizes": {},
    }

    print(f"\nvelune scan_directory benchmark  (native={NATIVE_AVAILABLE}, rounds={ROUNDS})")
    print("=" * 65)

    for label, n_files in tree_sizes.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_tree(root, n_files)

            print(f"\n  {label}")
            py_ms, py_count = bench("python  os.walk", _scan_directory_py, str(root), ROUNDS)

            if NATIVE_AVAILABLE:
                rs_ms, rs_count = bench("rust   velune_native", scan_directory, str(root), ROUNDS)

                if py_count != rs_count:
                    print(f"  ⚠ count mismatch: python={py_count}, rust={rs_count}")

                speedup = py_ms / rs_ms
                verdict = "✓ justified" if speedup >= THRESHOLD_SPEEDUP else "✗ not worth it"
                print(f"  speedup: {speedup:.2f}x  {verdict}")

                results["sizes"][label] = {
                    "py_ms": round(py_ms, 2),
                    "rs_ms": round(rs_ms, 2),
                    "speedup": round(speedup, 2),
                    "files": py_count,
                    "justified": speedup >= THRESHOLD_SPEEDUP,
                }
            else:
                results["sizes"][label] = {
                    "py_ms": round(py_ms, 2),
                    "rs_ms": None,
                    "speedup": None,
                    "files": py_count,
                    "justified": None,
                }

    return results


def check_results(results: dict, ci_mode: bool) -> int:
    if not NATIVE_AVAILABLE:
        print("\n[INFO] Rust extension not installed — Python fallback only.")
        return 0

    # Gate: Rust must be faster on trees with >= 5000 files
    large_sizes = ["5000 files", "10000 files"]
    failures = [
        s for s in large_sizes
        if results["sizes"].get(s, {}).get("justified") is False
    ]

    if failures and ci_mode:
        print(f"\n❌ FAIL: Rust scan_directory did not meet the {THRESHOLD_SPEEDUP:.0%} threshold")
        print(f"   Underperforming tree sizes: {failures}")
        print("   Keep the Python os.walk fallback; do NOT wire Rust scan_directory into indexer.")
        return 1

    if not failures:
        print(f"\n✓ Rust scan_directory meets the {THRESHOLD_SPEEDUP:.0%} threshold on large trees.")
        print("  Safe to wire _native.scan_directory into FilesystemScanner.")
    else:
        print(f"\n⚠ Rust scan_directory underperforms on: {failures}")
        print("  Keep Python os.walk. Re-run with --ci to gate on this in CI.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark native scan_directory")
    parser.add_argument("--ci", action="store_true", help="Exit non-zero if Rust is slower")
    parser.add_argument("--json", dest="json_out", metavar="FILE")
    args = parser.parse_args()

    results = run_benchmarks(ci_mode=args.ci)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nResults written to {out}")

    sys.exit(check_results(results, ci_mode=args.ci))


if __name__ == "__main__":
    main()
