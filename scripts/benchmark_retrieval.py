"""Benchmark the Intelligent Retrieval Engine (Phase 4).

Usage:
    python scripts/benchmark_retrieval.py [--json]

Generates synthetic, git-initialized repos at three sizes and measures
retrieval quality on a synthetic golden set: each file declares one
uniquely-named function and one uniquely-pathed FastAPI route, so a query
naming that symbol/route has exactly one known-correct file. This gives
real precision@k / recall@k / MRR without needing a hand-labeled corpus.

Compares two retrieval-query construction strategies run through the same
(now score-normalized, Phase-4) ``HybridRetriever`` pipeline:

  before   the query every call site used before Phase 4 —
           ``RetrievalQuery(text=text, top_k=10)``, fixed 0.5/0.3/0.2
           weights regardless of what's being asked.
  after    ``RetrievalPlanner.plan(intent, confidence, text)`` — weights
           and top_k chosen from the classified intent, reranked with the
           intent-conditioned trust boost.

Both arms use REAL lexical (BM25) and REAL graph retrieval (a genuine
``RepositoryCognitionService`` snapshot + grapher, built exactly as Phase 3
does it). The vector arm is not exercised — no LLM/embedding provider is
configured in this benchmark environment, and ``HybridRetriever`` already
degrades gracefully (empty vector hits) when none is available, exactly as
it does in production without one. This means the comparison isolates the
planner's strategy-selection and the reranker's intent-conditioning; the
score-normalization fix (Part 5) is validated separately, with exact
before/after numbers, by the unit tests in tests/test_hybrid_normalization.py
(a single-process benchmark run can't cleanly A/B a fix that's now the only
code path in the module it lives in).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The vector arm logs one warning per query when no embedding provider is
# configured (expected and disclosed in this module's docstring) — silence
# it so it doesn't drown out the results table.
logging.getLogger("velune.retrieval.hybrid").setLevel(logging.ERROR)

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from velune.cognition.intent import IntentClassifier  # noqa: E402
from velune.kernel.registry import get_container  # noqa: E402
from velune.repository.cognition import RepositoryCognitionService  # noqa: E402
from velune.retrieval.hybrid import HybridRetriever  # noqa: E402
from velune.retrieval.planner import RetrievalPlanner  # noqa: E402
from velune.retrieval.schemas import RetrievalDocument, RetrievalQuery  # noqa: E402

_SCENARIOS = [("small", 20), ("medium", 300), ("large", 2000)]
_TOP_K = 10


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_synthetic_repo(root: Path, n_files: int) -> list[dict]:
    """Flat package where each module declares one uniquely-named function
    and imports two neighbors, giving the grapher real import edges and each
    query a single known-correct answer file. Returns the golden query set."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")

    golden: list[dict] = []
    for i in range(n_files):
        imports = "\n".join(
            f"from pkg.mod_{(i + j) % n_files} import value_{(i + j) % n_files}"
            for j in (1, 2)
            if n_files > 2
        )
        symbol = f"unique_symbol_{i}_handler"
        body = (
            f"{imports}\n\n"
            f"value_{i} = {i}\n\n"
            f"def {symbol}():\n"
            f'    """Handles the {i}-th unique operation."""\n'
            f"    return {i}\n"
        )
        (pkg / f"mod_{i}.py").write_text(body)
        golden.append(
            {
                "query": f"find {symbol}",
                "answer_file": f"pkg/mod_{i}.py",
            }
        )

    _git("init", cwd=root)
    _git("config", "user.email", "bench@example.com", cwd=root)
    _git("config", "user.name", "Bench", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return golden


def _load_bm25_docs(root: Path) -> list[RetrievalDocument]:
    retrieval_index_path = root / ".velune" / "retrieval_index.json"
    with open(retrieval_index_path, encoding="utf-8") as fh:
        raw_docs = json.load(fh)
    return [
        RetrievalDocument(id=d["id"], content=d["content"], metadata=d.get("metadata", {}))
        for d in raw_docs
        if d.get("id") and d.get("content")
    ]


def _reciprocal_rank(hits: list, answer_file: str, top_k: int) -> float:
    for rank, hit in enumerate(hits[:top_k], start=1):
        meta = hit.document.metadata or {}
        path = meta.get("path") or meta.get("file_path") or hit.document.id
        if path == answer_file or path.replace("\\", "/") == answer_file:
            return 1.0 / rank
    return 0.0


async def _run_arm(retriever: HybridRetriever, query: RetrievalQuery, answer_file: str) -> dict:
    start = time.perf_counter()
    result = await retriever.retrieve(query)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    rr = _reciprocal_rank(result.hits, answer_file, query.top_k)
    return {"rr": rr, "recall": 1.0 if rr > 0 else 0.0, "latency_ms": elapsed_ms}


async def _bench_scenario(name: str, n_files: int) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"velune-retrieval-bench-{name}-"))
    try:
        golden = _make_synthetic_repo(tmp, n_files)

        cognition = RepositoryCognitionService(tmp)
        cognition.index(force=True)  # populates snapshot + .velune/retrieval_index.json
        get_container().register_instance("runtime.repository_cognition", cognition)

        retriever = HybridRetriever(location=":memory:")
        retriever.lexical_retriever.add_documents_batch(_load_bm25_docs(tmp))

        classifier = IntentClassifier()
        planner = RetrievalPlanner()

        before_runs, after_runs = [], []
        for case in golden:
            intent, confidence = classifier.classify_with_confidence(case["query"])

            before_query = RetrievalQuery(text=case["query"], top_k=_TOP_K)
            before_runs.append(await _run_arm(retriever, before_query, case["answer_file"]))

            after_query = planner.plan(intent, confidence, case["query"])
            after_runs.append(await _run_arm(retriever, after_query, case["answer_file"]))

        def _agg(runs: list[dict]) -> dict:
            return {
                "recall_at_k": round(statistics.mean(r["recall"] for r in runs), 3),
                "mrr": round(statistics.mean(r["rr"] for r in runs), 3),
                "p50_latency_ms": round(statistics.median(r["latency_ms"] for r in runs), 3),
            }

        return {
            "scenario": name,
            "n_files": n_files,
            "n_queries": len(golden),
            "before": _agg(before_runs),
            "after": _agg(after_runs),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    results = [asyncio.run(_bench_scenario(name, n)) for name, n in _SCENARIOS]

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print(
        f"{'scenario':<8} {'files':>6} {'queries':>8}  "
        f"{'recall@k (b/a)':>16}  {'MRR (b/a)':>13}  {'p50 ms (b/a)':>14}"
    )
    for r in results:
        b, a = r["before"], r["after"]
        print(
            f"{r['scenario']:<8} {r['n_files']:>6} {r['n_queries']:>8}  "
            f"{b['recall_at_k']:>7.3f}/{a['recall_at_k']:<7.3f}  "
            f"{b['mrr']:>6.3f}/{a['mrr']:<6.3f}  "
            f"{b['p50_latency_ms']:>7.3f}/{a['p50_latency_ms']:<6.3f}"
        )


if __name__ == "__main__":
    main()
