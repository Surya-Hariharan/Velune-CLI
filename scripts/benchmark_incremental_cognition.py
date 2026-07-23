"""Benchmark the incremental repository-cognition pipeline (Phase 3).

Usage:
    python scripts/benchmark_incremental_cognition.py [--json]

Generates synthetic, git-initialized repos at three sizes (small/medium/large)
and times three scenarios against ``RepositoryCognitionService``:

  cold        first ``get_snapshot_fresh()`` call — no pipeline cache yet, so
              this pays the full grapher/API-mapper/architecture/tech cost
              once (identical cost to the old per-turn behavior).
  warm_noop   a second call with nothing changed on disk — this is what every
              "no edits since the last prompt" turn now costs. Before this
              phase, every turn paid the ``cold`` cost; after, it's this.
  after_edit  one file is edited, then the background ``refresh_pipeline_cache``
              path (not the interactive path) is timed, followed by a
              ``get_snapshot_fresh()`` call that must read the now-updated
              cache without recomputing anything itself.

The point of the comparison is the *ratio* between ``cold`` and
``warm_noop``/``after_edit`` at each repo size — it should grow with repo
size, demonstrating that update cost scales with the size of the change, not
the size of the repository, exactly as the old unconditional full-rebuild-
per-prompt behavior did not.

This is a synthetic, local, single-process benchmark — no real 20k-file
enterprise repository was available to test against, and none is fabricated
here beyond flat synthetic files with a few cross-imports each. Scoped
deliberately to what's realistic to measure for a local CLI process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from velune.repository.cognition import RepositoryCognitionService  # noqa: E402
from velune.repository.incremental_indexer import IncrementalIndexer  # noqa: E402

_SCENARIOS = [
    ("small", 30),
    ("medium", 500),
    ("large", 5000),
]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_synthetic_repo(root: Path, n_files: int) -> None:
    """Flat package of *n_files* Python modules, each importing ~2 neighbors
    and declaring one FastAPI route, so the grapher and API mapper both have
    real work to do — not just an empty tree."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")

    for i in range(n_files):
        imports = "\n".join(
            f"from pkg.mod_{(i + j) % n_files} import value_{(i + j) % n_files}"
            for j in (1, 2)
            if n_files > 2
        )
        body = (
            f"{imports}\n\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            f"value_{i} = {i}\n\n"
            f'@app.get("/items/{i}")\n'
            f"def get_item_{i}():\n"
            f"    return {{'id': {i}}}\n"
        )
        (pkg / f"mod_{i}.py").write_text(body)

    _git("init", cwd=root)
    _git("config", "user.email", "bench@example.com", cwd=root)
    _git("config", "user.name", "Bench", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "init", cwd=root)


async def _time(coro) -> float:
    start = time.perf_counter()
    await coro
    return time.perf_counter() - start


async def _bench_one(root: Path, n_files: int) -> dict:
    service = RepositoryCognitionService(root)

    cold_t = await _time(service.get_snapshot_fresh())

    warm_t = await _time(service.get_snapshot_fresh())

    (root / "pkg" / "mod_0.py").write_text("value_0 = 999  # edited for benchmark\n")
    state_path = root / ".velune" / "index_state.json"
    inc = IncrementalIndexer(root, state_path)
    delta = await inc.compute_delta()
    await inc.apply_delta(delta)

    refresh_start = time.perf_counter()
    await service.refresh_pipeline_cache(delta)
    refresh_t = time.perf_counter() - refresh_start

    after_edit_t = await _time(service.get_snapshot_fresh())

    return {
        "n_files": n_files,
        "cold_s": round(cold_t, 4),
        "warm_noop_s": round(warm_t, 4),
        "background_refresh_s": round(refresh_t, 4),
        "after_edit_read_s": round(after_edit_t, 4),
        "warm_speedup_x": round(cold_t / warm_t, 1) if warm_t > 0 else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    results = []
    for name, n_files in _SCENARIOS:
        tmp = Path(tempfile.mkdtemp(prefix=f"velune-bench-{name}-"))
        try:
            _make_synthetic_repo(tmp, n_files)
            stats = asyncio.run(_bench_one(tmp, n_files))
            stats["scenario"] = name
            results.append(stats)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print(
        f"{'scenario':<8} {'files':>6} {'cold':>8} {'warm_noop':>10} {'speedup':>9} "
        f"{'bg_refresh':>11} {'after_edit':>11}"
    )
    for r in results:
        print(
            f"{r['scenario']:<8} {r['n_files']:>6} {r['cold_s']:>8.4f} "
            f"{r['warm_noop_s']:>10.4f} {str(r['warm_speedup_x']) + 'x':>9} "
            f"{r['background_refresh_s']:>11.4f} {r['after_edit_read_s']:>11.4f}"
        )


if __name__ == "__main__":
    main()
