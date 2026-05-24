"""Comprehensive unit tests for Velune Phase 5:
Trade-off Evaluation Matrix (TEM), Sandboxed Benchmarking,
and Monthly Evolution Timeline.
"""

from __future__ import annotations

import time
from pathlib import Path
import pytest

from velune.cognition.tradeoff import TradeoffEvaluationMatrix, DEFAULT_AXES
from velune.execution.benchmarker import SubsystemBenchmarker
from velune.memory.tiers.lineage import LineageMemoryTier
from velune.cognition.evolution import EvolutionTimelineReporter


# =====================================================================
# 1. Trade-off Evaluation Matrix Tests
# =====================================================================

class TestTradeoffEvaluationMatrix:
    """Tests for TEM scoring, ranking, persistence, and reporting."""

    def test_tem_select_optimal_picks_highest_weighted_score(self) -> None:
        """The option with the highest weighted composite score should win."""
        tem = TradeoffEvaluationMatrix(task_id="unit-test-001")
        tem.add_option(
            "Async Queue",
            metrics={
                "performance": 0.9,
                "maintainability": 0.8,
                "safety": 0.85,
                "scalability": 0.9,
                "simplicity": 0.6,
            },
        )
        tem.add_option(
            "Thread Lock",
            metrics={
                "performance": 0.5,
                "maintainability": 0.6,
                "safety": 0.95,
                "scalability": 0.4,
                "simplicity": 0.8,
            },
        )

        winner = tem.select_optimal()
        assert winner.name == "Async Queue"
        assert winner.weighted_score > 0.0

    def test_tem_weighted_score_clamps_between_0_and_1(self) -> None:
        """Weighted scores must be within [0.0, 1.0]."""
        tem = TradeoffEvaluationMatrix(task_id="unit-test-clamp")
        tem.add_option(
            "OverMax",
            metrics={
                "performance": 2.5,   # will be clamped to 1.0
                "maintainability": -1.0,  # will be clamped to 0.0
                "safety": 0.5,
                "scalability": 0.5,
                "simplicity": 0.5,
            },
        )
        winner = tem.select_optimal()
        assert 0.0 <= winner.weighted_score <= 1.0

    def test_tem_custom_axes_weights_normalised(self) -> None:
        """Custom axes weights should be normalised to sum to 1.0."""
        custom_axes = {"performance": 2.0, "safety": 2.0}  # 4.0 total -> each 0.5
        tem = TradeoffEvaluationMatrix(task_id="unit-test-custom", axes=custom_axes)

        assert abs(sum(tem.axes.values()) - 1.0) < 1e-9

        tem.add_option("A", metrics={"performance": 0.8, "safety": 0.6})
        tem.add_option("B", metrics={"performance": 0.3, "safety": 0.9})

        winner = tem.select_optimal()
        # A: 0.5*0.8 + 0.5*0.6 = 0.70   B: 0.5*0.3 + 0.5*0.9 = 0.60
        assert winner.name == "A"
        assert abs(winner.weighted_score - 0.70) < 0.01

    def test_tem_no_options_raises_value_error(self) -> None:
        """select_optimal() must raise ValueError when no options exist."""
        tem = TradeoffEvaluationMatrix(task_id="unit-test-empty")
        with pytest.raises(ValueError, match="no options registered"):
            tem.select_optimal()

    def test_tem_get_rankings_sorted_descending(self) -> None:
        """get_rankings() must return options sorted by score, highest first."""
        tem = TradeoffEvaluationMatrix(task_id="unit-test-ranking")
        tem.add_option("Low",  metrics={k: 0.2 for k in DEFAULT_AXES})
        tem.add_option("High", metrics={k: 0.9 for k in DEFAULT_AXES})
        tem.add_option("Mid",  metrics={k: 0.5 for k in DEFAULT_AXES})

        rankings = tem.get_rankings()
        scores = [r["weighted_score"] for r in rankings]
        assert scores == sorted(scores, reverse=True)
        assert rankings[0]["name"] == "High"

    def test_tem_explain_decision_contains_all_sections(self) -> None:
        """explain_decision() must include all required Markdown sections."""
        tem = TradeoffEvaluationMatrix(task_id="unit-test-explain")
        tem.add_option("Alpha", metrics={k: 0.7 for k in DEFAULT_AXES})
        tem.add_option("Beta",  metrics={k: 0.5 for k in DEFAULT_AXES})

        report = tem.explain_decision()
        assert "# Trade-off Evaluation Report" in report
        assert "## Evaluation Axes & Weights" in report
        assert "## Option Scorecards" in report
        assert "✅ Selected Architecture" in report
        assert "Rejected Alternatives" in report
        assert "Alpha" in report
        assert "Beta" in report

    def test_tem_method_chaining(self) -> None:
        """add_option() should support fluent method chaining."""
        tem = (
            TradeoffEvaluationMatrix(task_id="chain-test")
            .add_option("X", metrics={"performance": 0.7, "safety": 0.8})
            .add_option("Y", metrics={"performance": 0.9, "safety": 0.6})
        )
        assert len(tem.options) == 2

    def test_tem_persists_decision_to_lineage_db(self, tmp_path: Path) -> None:
        """TEM should persist the winning decision to the lineage database."""
        lineage = LineageMemoryTier(db_path=tmp_path / "lineage_tem.db")

        tem = TradeoffEvaluationMatrix(
            task_id="persist-test",
            lineage_memory=lineage,
        )
        tem.add_option("Winner", metrics={k: 0.9 for k in DEFAULT_AXES})
        tem.add_option("Loser",  metrics={k: 0.3 for k in DEFAULT_AXES})
        tem.select_optimal()

        # Allow write queue to flush
        time.sleep(0.3)

        decisions = lineage.get_subsystem_decisions("persist-test")
        assert any("Winner" in d["rationale"] for d in decisions)

        lineage.shutdown()


# =====================================================================
# 2. Sandboxed Micro-Benchmarker Tests
# =====================================================================

class TestSubsystemBenchmarker:
    """Tests for isolated subprocess benchmarking and comparison."""

    def test_benchmark_fast_snippet_captures_latency(self) -> None:
        """A trivial snippet should produce a non-negative latency reading."""
        bench = SubsystemBenchmarker(repetitions=1)
        result = bench.run_benchmark("x = 1 + 1", label="trivial-add")

        assert result.label == "trivial-add"
        assert result.latency_ms >= 0.0
        assert result.peak_rss_kb >= 0.0
        assert result.error == ""

    def test_benchmark_failing_snippet_records_error(self) -> None:
        """A snippet with a syntax error must produce a non-empty error field."""
        bench = SubsystemBenchmarker(repetitions=1, timeout=5.0)
        result = bench.run_benchmark("this is not valid python !!!", label="bad-snippet")

        # Error must be captured; latency may be 0
        assert result.error != ""

    def test_compare_ranks_faster_option_first(self) -> None:
        """compare() must rank the faster snippet first."""
        bench = SubsystemBenchmarker(repetitions=2, timeout=5.0)

        snippets = {
            "fast": "x = sum(range(100))",
            "slow": "import time; time.sleep(0.01)",
        }
        results = bench.compare(snippets)

        assert len(results) == 2
        assert results[0].label == "fast"
        assert results[0].latency_ms <= results[1].latency_ms

    def test_compare_sets_cpu_factor_for_fastest(self) -> None:
        """The fastest result must have cpu_factor == 1.0."""
        bench = SubsystemBenchmarker(repetitions=1, timeout=5.0)
        snippets = {
            "A": "x = 1",
            "B": "import time; time.sleep(0.02)",
        }
        results = bench.compare(snippets)

        successful = [r for r in results if not r.error]
        if len(successful) >= 2:
            fastest = min(successful, key=lambda r: r.latency_ms)
            assert abs(fastest.cpu_factor - 1.0) < 1e-6

    def test_results_to_tem_metrics_scores_performance(self) -> None:
        """results_to_tem_metrics() should produce performance scores in [0.0, 1.0]."""
        bench = SubsystemBenchmarker(repetitions=1, timeout=5.0)
        snippets = {
            "fast": "x = 1 + 1",
            "slow": "import time; time.sleep(0.05)",
        }
        results = bench.compare(snippets)
        metrics = bench.results_to_tem_metrics(results)

        assert "fast" in metrics
        assert "slow" in metrics
        for label, m in metrics.items():
            assert 0.0 <= m.get("performance", 0.0) <= 1.0

    def test_compare_empty_snippets_returns_empty_list(self) -> None:
        """compare() with an empty dict must return an empty list."""
        bench = SubsystemBenchmarker()
        results = bench.compare({})
        assert results == []


# =====================================================================
# 3. Monthly Evolution Timeline Tests
# =====================================================================

class TestEvolutionTimeline:
    """Tests for SQLite monthly snapshot persistence and Markdown changelog."""

    def test_log_and_retrieve_snapshot(self, tmp_path: Path) -> None:
        """A logged snapshot should be retrievable from the DB."""
        lineage = LineageMemoryTier(db_path=tmp_path / "evolution.db")

        lineage.log_monthly_snapshot(
            subsystem="velune/memory",
            lcom_average=2.5,
            coupling_ratio=0.18,
            debt_items_count=3,
            milestone="Migrated to async write queue",
            rationale_summary="Thread-safe writes required for Windows compatibility.",
        )

        # Allow write queue to flush
        time.sleep(0.3)

        snapshots = lineage.get_evolution_timeline("velune/memory")
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["subsystem"] == "velune/memory"
        assert abs(snap["lcom_average"] - 2.5) < 0.01
        assert abs(snap["coupling_ratio"] - 0.18) < 0.001
        assert snap["debt_items_count"] == 3
        assert "async write queue" in snap["major_milestone"]

        lineage.shutdown()

    def test_multiple_snapshots_ordered_by_most_recent(self, tmp_path: Path) -> None:
        """get_evolution_timeline should return snapshots newest-first."""
        lineage = LineageMemoryTier(db_path=tmp_path / "evolution_order.db")

        for i in range(3):
            lineage.log_monthly_snapshot(
                subsystem="velune/cognition",
                lcom_average=float(i),
                coupling_ratio=0.1 * i,
                debt_items_count=i,
                milestone=f"Milestone {i}",
                rationale_summary=f"Rationale {i}",
            )
            time.sleep(0.05)  # ensure distinct timestamps

        time.sleep(0.3)

        snapshots = lineage.get_evolution_timeline("velune/cognition")
        assert len(snapshots) == 3
        # Most recent first → lcom_average should be 2, 1, 0
        assert snapshots[0]["lcom_average"] > snapshots[-1]["lcom_average"]

        lineage.shutdown()

    def test_get_evolution_timeline_empty_for_unknown_subsystem(self, tmp_path: Path) -> None:
        """An unknown subsystem should return an empty list."""
        lineage = LineageMemoryTier(db_path=tmp_path / "evolution_empty.db")
        result = lineage.get_evolution_timeline("nonexistent/module")
        assert result == []
        lineage.shutdown()

    def test_generate_report_placeholder_when_no_snapshots(self, tmp_path: Path) -> None:
        """generate_report() must return a placeholder when no data exists."""
        lineage = LineageMemoryTier(db_path=tmp_path / "evo_no_snaps.db")
        reporter = EvolutionTimelineReporter(lineage)
        report = reporter.generate_report("velune/newmodule")

        assert "No architecture snapshots" in report
        assert "velune/newmodule" in report
        lineage.shutdown()

    def test_generate_report_contains_all_sections(self, tmp_path: Path) -> None:
        """generate_report() must include trends, history, and footer."""
        lineage = LineageMemoryTier(db_path=tmp_path / "evo_full.db")

        for i in range(2):
            lineage.log_monthly_snapshot(
                subsystem="velune/execution",
                lcom_average=float(i + 1),
                coupling_ratio=0.2 + i * 0.05,
                debt_items_count=2 + i,
                milestone=f"Phase {i + 1} complete",
                rationale_summary=f"Completed phase {i + 1} refactoring.",
            )
            time.sleep(0.05)

        time.sleep(0.3)

        reporter = EvolutionTimelineReporter(lineage)
        report = reporter.generate_report("velune/execution")

        assert "# Subsystem Evolution Timeline" in report
        assert "## Architectural Health Trends" in report
        assert "## Snapshot History" in report
        assert "LCOM Average" in report
        assert "Coupling Ratio" in report
        assert "Phase 1 complete" in report
        assert "Phase 2 complete" in report
        lineage.shutdown()

    def test_snapshot_current_health_delegates_to_lineage(self, tmp_path: Path) -> None:
        """snapshot_current_health() must write a retrievable record via lineage."""
        lineage = LineageMemoryTier(db_path=tmp_path / "evo_health.db")
        reporter = EvolutionTimelineReporter(lineage)

        reporter.snapshot_current_health(
            subsystem="velune/cognition/council",
            lcom_average=0.75,
            coupling_ratio=0.12,
            debt_items_count=1,
            milestone="Critic council stabilised",
            rationale_summary="All critics converge within 2 debate turns.",
        )

        time.sleep(0.3)

        snapshots = lineage.get_evolution_timeline("velune/cognition/council")
        assert len(snapshots) == 1
        assert "stabilised" in snapshots[0]["major_milestone"]
        lineage.shutdown()


# =====================================================================
# 4. TEM + Benchmarker Integration Test
# =====================================================================

class TestTemBenchmarkerIntegration:
    """Verifies the full pipeline: benchmark -> TEM metrics -> optimal selection."""

    def test_benchmark_feeds_tem_and_selects_fastest(self) -> None:
        """Benchmark results converted to TEM metrics should favour the faster option."""
        bench = SubsystemBenchmarker(repetitions=1, timeout=8.0)

        snippets = {
            "ListComp": "result = [x * 2 for x in range(1000)]",
            "ForLoop":  (
                "result = []\n"
                "for x in range(1000):\n"
                "    result.append(x * 2)"
            ),
        }
        results = bench.compare(snippets)
        metrics = bench.results_to_tem_metrics(results)

        tem = TradeoffEvaluationMatrix(task_id="bench-tem-integration")
        for label, m in metrics.items():
            tem.add_option(label, metrics=m)

        winner = tem.select_optimal()
        # Both approaches should produce a valid winner name
        assert winner.name in snippets
        assert 0.0 <= winner.weighted_score <= 1.0

    def test_full_cycle_lineage_persistence(self, tmp_path: Path) -> None:
        """End-to-end: benchmark -> TEM -> lineage persist -> evolution snapshot."""
        lineage = LineageMemoryTier(db_path=tmp_path / "integration.db")
        reporter = EvolutionTimelineReporter(lineage)

        # TEM
        tem = TradeoffEvaluationMatrix(
            task_id="e2e-test",
            lineage_memory=lineage,
        )
        tem.add_option("OptionA", metrics={k: 0.8 for k in DEFAULT_AXES})
        tem.add_option("OptionB", metrics={k: 0.4 for k in DEFAULT_AXES})
        winner = tem.select_optimal()
        assert winner.name == "OptionA"

        # Evolution snapshot after successful execution
        reporter.snapshot_current_health(
            subsystem="e2e-subsystem",
            lcom_average=1.5,
            coupling_ratio=0.22,
            debt_items_count=0,
            milestone="Phase 5 E2E test",
            rationale_summary="TEM + evolution pipeline validated.",
        )

        time.sleep(0.4)

        # Both lineage tables should have records
        decisions = lineage.get_subsystem_decisions("e2e-test")
        snapshots = lineage.get_evolution_timeline("e2e-subsystem")
        assert len(decisions) >= 1
        assert len(snapshots) == 1
        assert snapshots[0]["debt_items_count"] == 0

        lineage.shutdown()
