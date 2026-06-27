#!/usr/bin/env python3
"""Benchmark: Rust native sha256_file vs Python hashlib baseline.

Run with:
    python scripts/benchmark_native.py

Or in CI (where the Rust wheel is built and installed):
    pip install ext/rust/velune-native/
    python scripts/benchmark_native.py --ci

The script exits non-zero if Rust is available but slower than Python —
that would mean the native extension is not earning its build complexity.

Decision rule (see architecture notes):
    Rust is only worth the polyglot cost if it provides >= 20% throughput
    improvement on files >= 1 MB.  Below that threshold the Python fallback
    is retained and the Rust build is removed at the next release cut.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import timeit
from pathlib import Path

# Ensure the repo root is on sys.path when run directly.
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from velune.repository._native import (  # noqa: E402
    NATIVE_AVAILABLE,
    _sha256_file_py,
    sha256_file,
)

THRESHOLD_SPEEDUP = 1.20  # Rust must be >= 20% faster to justify its cost
ROUNDS = 20


def make_temp_file(size_bytes: int) -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    # Pseudo-random bytes — not compressible, realistic for source files
    chunk = os.urandom(min(size_bytes, 65536))
    written = 0
    while written < size_bytes:
        n = min(len(chunk), size_bytes - written)
        f.write(chunk[:n])
        written += n
    f.close()
    return f.name


def bench(label: str, fn, path: str, rounds: int) -> float:
    """Return throughput in MB/s."""
    size_mb = os.path.getsize(path) / (1024 * 1024)
    elapsed = timeit.timeit(lambda: fn(path), number=rounds)
    mb_s = (size_mb * rounds) / elapsed
    print(f"  {label:<28} {mb_s:>8.1f} MB/s  ({elapsed / rounds * 1000:.2f} ms/call)")
    return mb_s


def run_benchmarks(ci_mode: bool) -> dict:
    sizes = {
        "4 KB": 4 * 1024,
        "64 KB": 64 * 1024,
        "1 MB": 1 * 1024 * 1024,
        "8 MB": 8 * 1024 * 1024,
        "32 MB": 32 * 1024 * 1024,
    }

    results: dict = {
        "native_available": NATIVE_AVAILABLE,
        "threshold_speedup": THRESHOLD_SPEEDUP,
        "sizes": {},
    }

    print(f"\nvelune sha256 benchmark  (native={NATIVE_AVAILABLE}, rounds={ROUNDS})")
    print("=" * 60)

    for label, size in sizes.items():
        path = make_temp_file(size)
        try:
            print(f"\n  {label} ({size:,} bytes)")
            py_mbs = bench("python  hashlib.sha256", _sha256_file_py, path, ROUNDS)

            if NATIVE_AVAILABLE:
                rs_mbs = bench("rust   velune_native", sha256_file, path, ROUNDS)
                speedup = rs_mbs / py_mbs
                verdict = "✓ justified" if speedup >= THRESHOLD_SPEEDUP else "✗ not worth it"
                print(f"  speedup: {speedup:.2f}x  {verdict}")
                results["sizes"][label] = {
                    "py_mbs": round(py_mbs, 1),
                    "rs_mbs": round(rs_mbs, 1),
                    "speedup": round(speedup, 2),
                    "justified": speedup >= THRESHOLD_SPEEDUP,
                }
            else:
                results["sizes"][label] = {
                    "py_mbs": round(py_mbs, 1),
                    "rs_mbs": None,
                    "speedup": None,
                    "justified": None,
                }
        finally:
            os.unlink(path)

    return results


def check_results(results: dict, ci_mode: bool) -> int:
    if not NATIVE_AVAILABLE:
        print("\n[INFO] Rust extension not installed — Python fallback only.")
        print("       Install with: pip install ext/rust/velune-native/")
        return 0

    # Gate: Rust must be faster on files >= 1 MB
    large_file_sizes = ["1 MB", "8 MB", "32 MB"]
    failures = [
        s for s in large_file_sizes
        if results["sizes"].get(s, {}).get("justified") is False
    ]

    if failures and ci_mode:
        print(f"\n❌ FAIL: Rust sha256 did not meet the {THRESHOLD_SPEEDUP:.0%} threshold")
        print(f"   Underperforming sizes: {failures}")
        print("   Consider removing the Rust sha256 and keeping the Python fallback.")
        return 1

    if not failures:
        print(f"\n✓ Rust sha256 meets the {THRESHOLD_SPEEDUP:.0%} speedup threshold on large files.")
    else:
        print(f"\n⚠ Rust sha256 underperforms on: {failures}")
        print("  Not failing in non-CI mode — re-run with --ci to gate on this.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark native sha256")
    parser.add_argument("--ci", action="store_true", help="Exit non-zero if Rust is slower")
    parser.add_argument(
        "--json", dest="json_out", metavar="FILE", help="Write results to JSON file"
    )
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
