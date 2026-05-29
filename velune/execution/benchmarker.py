"""Sandboxed Micro-Benchmarker for subsystem performance profiling.

Executes user-supplied Python code snippets inside isolated subprocesses
and captures wall-clock latency, CPU time, and peak RSS memory.  Results
feed directly into the Trade-off Evaluation Matrix (TEM) so Velune can
select architectures based on empirical data rather than assumptions.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.execution.benchmarker")

# Timeout ceiling per individual snippet run (seconds).
_DEFAULT_TIMEOUT: float = 10.0

# Number of repetitions to average latency over.
_DEFAULT_REPS: int = 3

_HARNESS_TEMPLATE = textwrap.dedent(
    """\
    import time
    import tracemalloc

    tracemalloc.start()
    _t0 = time.perf_counter()

    {snippet}

    _elapsed = time.perf_counter() - _t0
    _, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"VELUNE_BENCH:latency_ms={{_elapsed * 1000:.4f}}")
    print(f"VELUNE_BENCH:peak_rss_kb={{_peak / 1024:.2f}}")
    """
)


class BenchmarkResult:
    """Holds the captured metrics for a single snippet run.

    Attributes:
        label: Human-readable name for the snippet / alternative.
        latency_ms: Average wall-clock time across repetitions, in milliseconds.
        peak_rss_kb: Peak RSS memory observed during the run, in kilobytes.
        cpu_factor: Normalised indicator derived from latency relative to fastest
            option (1.0 = fastest, >1.0 = slower).
        error: Non-empty string if the snippet failed to execute cleanly.
    """

    def __init__(
        self,
        label: str,
        latency_ms: float,
        peak_rss_kb: float,
        error: str = "",
    ) -> None:
        self.label = label
        self.latency_ms = latency_ms
        self.peak_rss_kb = peak_rss_kb
        self.cpu_factor: float = 1.0     # set by SubsystemBenchmarker.compare()
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dictionary.

        Returns:
            Dict containing all benchmark metrics for this result.
        """
        return {
            "label": self.label,
            "latency_ms": round(self.latency_ms, 4),
            "peak_rss_kb": round(self.peak_rss_kb, 2),
            "cpu_factor": round(self.cpu_factor, 4),
            "error": self.error,
        }

    def __repr__(self) -> str:
        return (
            f"BenchmarkResult(label={self.label!r}, "
            f"latency_ms={self.latency_ms:.2f}, "
            f"peak_rss_kb={self.peak_rss_kb:.1f})"
        )


class SubsystemBenchmarker:
    """Executes code snippets in isolated subprocesses and captures performance metrics.

    Each snippet is wrapped in a timing + memory-tracing harness and run in a
    fresh Python interpreter, preventing any side effects from leaking between
    alternatives.

    Args:
        timeout: Maximum wall-clock seconds allowed per individual snippet run.
        repetitions: Number of times each snippet is repeated; metrics are averaged.
        workspace: Optional working directory for the subprocess.  Defaults to cwd.
    """

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        repetitions: int = _DEFAULT_REPS,
        workspace: Path | None = None,
    ) -> None:
        self.timeout = timeout
        self.repetitions = max(1, repetitions)
        self.workspace = workspace or Path.cwd()

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def run_benchmark(self, code_snippet: str, label: str) -> BenchmarkResult:
        """Execute a single code snippet and return captured metrics.

        Args:
            code_snippet: Valid Python source code to benchmark.  It will be
                executed at module level inside an isolated subprocess.
            label: Human-readable identifier for this alternative (e.g. ``"AsyncQueue"``).

        Returns:
            A ``BenchmarkResult`` with averaged latency and peak RSS memory.
        """
        latencies: list[float] = []
        peak_rss_values: list[float] = []
        last_error = ""

        for rep in range(self.repetitions):
            latency_ms, peak_rss_kb, err = self._run_once(code_snippet, label, rep)
            if err:
                last_error = err
                # Still record what we have; a 0.0 result will drag averages but
                # the ``error`` field signals the failure clearly.
                latencies.append(0.0)
                peak_rss_values.append(0.0)
            else:
                latencies.append(latency_ms)
                peak_rss_values.append(peak_rss_kb)

        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        avg_peak_rss = sum(peak_rss_values) / len(peak_rss_values) if peak_rss_values else 0.0

        result = BenchmarkResult(
            label=label,
            latency_ms=avg_latency,
            peak_rss_kb=avg_peak_rss,
            error=last_error,
        )
        logger.info(
            "Benchmark '%s': avg_latency=%.3fms, avg_peak_rss=%.1fKB",
            label,
            avg_latency,
            avg_peak_rss,
        )
        return result

    def compare(self, snippets: dict[str, str]) -> list[BenchmarkResult]:
        """Benchmark multiple alternatives and return results ranked by latency.

        After benchmarking, each result's ``cpu_factor`` is normalised relative to
        the fastest option (cpu_factor == 1.0) so callers can feed scores directly
        into a ``TradeoffEvaluationMatrix``.

        Args:
            snippets: Mapping of label to Python source code snippet.

        Returns:
            List of ``BenchmarkResult`` objects, sorted from fastest to slowest.
        """
        if not snippets:
            logger.warning("compare() called with empty snippets dict.")
            return []

        results: list[BenchmarkResult] = []
        for label, code in snippets.items():
            result = self.run_benchmark(code, label)
            results.append(result)

        # Compute cpu_factor relative to the fastest (lowest) latency
        successful = [r for r in results if not r.error]
        if successful:
            fastest = min(r.latency_ms for r in successful)
            for r in results:
                if r.latency_ms > 0 and not r.error:
                    r.cpu_factor = r.latency_ms / fastest
                else:
                    r.cpu_factor = float("inf") if r.error else 1.0

        results.sort(key=lambda r: r.latency_ms if not r.error else float("inf"))
        return results

    def results_to_tem_metrics(self, results: list[BenchmarkResult]) -> dict[str, dict[str, float]]:
        """Convert ranked benchmark results into TEM-compatible metric dicts.

        For each result, scores ``performance`` and ``simplicity`` axes on a
        [0.0, 1.0] scale.  The caller can supplement these with domain-specific
        expert scores for the remaining axes before calling
        ``TradeoffEvaluationMatrix.add_option()``.

        Args:
            results: Output of ``compare()``.

        Returns:
            Dict mapping label -> TEM metric dict.
        """
        if not results:
            return {}

        max_lat = max((r.latency_ms for r in results if not r.error), default=1.0)
        max_rss = max((r.peak_rss_kb for r in results if not r.error), default=1.0)

        metrics: dict[str, dict[str, float]] = {}
        for r in results:
            if r.error:
                metrics[r.label] = {"performance": 0.0, "safety": 0.0}
            else:
                # performance: inverted normalised latency (lower latency -> higher score)
                performance = 1.0 - (r.latency_ms / max_lat) if max_lat > 0 else 0.5
                # memory proxy: inverted normalised peak RSS
                memory_score = 1.0 - (r.peak_rss_kb / max_rss) if max_rss > 0 else 0.5
                metrics[r.label] = {
                    "performance": round(performance, 4),
                    "safety": round(memory_score, 4),
                }
        return metrics

    # ─────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────

    def _run_once(
        self, code_snippet: str, label: str, rep: int
    ) -> tuple[float, float, str]:
        """Execute the snippet once and parse the harness output.

        Returns:
            Tuple of (latency_ms, peak_rss_kb, error_string).
        """
        harness = _HARNESS_TEMPLATE.format(snippet=textwrap.dedent(code_snippet))

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(harness)
            tmp_path = tmp.name

        try:
            run_env = {**os.environ}
            for dangerous in (
                "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH",
                "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT",
                "BASH_ENV", "ENV", "PROMPT_COMMAND"
            ):
                run_env.pop(dangerous, None)
            run_env["PYTHONNOUSERSITE"] = "1"

            with tempfile.TemporaryDirectory() as temp_cwd:
                proc = subprocess.run(
                    [sys.executable, tmp_path],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=temp_cwd,
                    env=run_env,
                )

            if proc.returncode != 0:
                err_text = (proc.stderr or proc.stdout or "unknown error").strip()
                logger.warning("Benchmark '%s' rep %d failed: %s", label, rep, err_text[:200])
                return 0.0, 0.0, err_text[:500]

            latency_ms = 0.0
            peak_rss_kb = 0.0
            for line in proc.stdout.splitlines():
                if line.startswith("VELUNE_BENCH:latency_ms="):
                    try:
                        latency_ms = float(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif line.startswith("VELUNE_BENCH:peak_rss_kb="):
                    try:
                        peak_rss_kb = float(line.split("=", 1)[1])
                    except ValueError:
                        pass

            return latency_ms, peak_rss_kb, ""

        except subprocess.TimeoutExpired:
            err = f"Timeout after {self.timeout}s"
            logger.warning("Benchmark '%s' rep %d timed out.", label, rep)
            return 0.0, 0.0, err
        except Exception as exc:
            logger.error("Benchmark '%s' rep %d raised: %s", label, rep, exc)
            return 0.0, 0.0, str(exc)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
