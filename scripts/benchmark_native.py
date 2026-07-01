"""Benchmark velune_native (Rust) against its pure-Python fallback.

Usage:
    python scripts/benchmark_native.py [root_dir] [--repeats N] [--sample-size N] [--json]

Compares wall-clock time for ``sha256_file`` and ``scan_directory`` between
the compiled Rust extension (if installed) and the pure-Python fallback in
velune/repository/_native.py. Both paths return identical results by
contract, so any measured delta is purely native-vs-interpreted cost — this
is the evidence velune/repository/_native.py's docstring says is needed
before treating Rust as the default for either function.

If the ``velune-native`` wheel isn't installed, only the Python fallback is
timed and the report notes that no comparison is available.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from velune.repository import _native  # noqa: E402
from velune.repository.scanner import CODE_EXTENSIONS  # noqa: E402

_SKIP_DIRS = [
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
]


def _time_calls(fn, args: tuple, repeats: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn(*args)
        samples.append(time.perf_counter() - start)
    return {
        "min_s": round(min(samples), 4),
        "median_s": round(statistics.median(samples), 4),
        "max_s": round(max(samples), 4),
    }


def bench_scan_directory(root: str, repeats: int) -> dict:
    extensions = list(CODE_EXTENSIONS)
    result: dict = {
        "python_fallback": _time_calls(
            _native._scan_directory_py, (root, extensions, _SKIP_DIRS), repeats
        ),
    }
    if _native.NATIVE_AVAILABLE:
        result["rust_native"] = _time_calls(
            _native._rust.scan_directory, (root, extensions, _SKIP_DIRS), repeats
        )
    return result


def bench_sha256_file(root: str, repeats: int, sample_size: int) -> dict:
    files = _native._scan_directory_py(root, [], _SKIP_DIRS)[:sample_size]
    if not files:
        return {"error": f"no files found under {root}"}

    def hash_all_python() -> None:
        for f in files:
            _native._sha256_file_py(f)

    result: dict = {
        "files_hashed": len(files),
        "python_fallback": _time_calls(hash_all_python, (), repeats),
    }
    if _native.NATIVE_AVAILABLE:

        def hash_all_native() -> None:
            for f in files:
                _native._rust.sha256_file(f)

        result["rust_native"] = _time_calls(hash_all_native, (), repeats)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default=str(_REPO_ROOT),
        help="Directory to benchmark (default: repo root)",
    )
    parser.add_argument("--repeats", type=int, default=5, help="Timing samples per benchmark")
    parser.add_argument(
        "--sample-size", type=int, default=500, help="Max files to hash for the sha256 benchmark"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of a text table"
    )
    args = parser.parse_args()

    results = {
        "native_extension_installed": _native.NATIVE_AVAILABLE,
        "root": args.root,
        "scan_directory": bench_scan_directory(args.root, args.repeats),
        "sha256_file": bench_sha256_file(args.root, args.repeats, args.sample_size),
    }

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print(f"velune_native benchmark — root={args.root}")
    print(f"Rust extension installed: {results['native_extension_installed']}")
    print()
    for name in ("scan_directory", "sha256_file"):
        print(f"== {name} ==")
        data = results[name]
        if "error" in data:
            print(f"  {data['error']}")
            print()
            continue
        if "files_hashed" in data:
            print(f"  files hashed: {data['files_hashed']}")
        for variant in ("python_fallback", "rust_native"):
            if variant in data:
                stats = data[variant]
                print(
                    f"  {variant:16s} min={stats['min_s']}s  "
                    f"median={stats['median_s']}s  max={stats['max_s']}s"
                )
        if "rust_native" not in data:
            print("  (Rust extension not installed — `pip install velune-native` to compare)")
        print()


if __name__ == "__main__":
    main()
