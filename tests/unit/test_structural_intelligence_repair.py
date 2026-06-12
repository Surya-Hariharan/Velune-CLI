"""Unit tests for Velune Blast Radius & Structural Intelligence Phase 1."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from velune.cognition.arbitrator import CouncilArbitrator
from velune.cognition.council.tiers import CouncilTier, TierClassifier
from velune.cognition.orchestrator import CouncilOrchestrator
from velune.kernel.registry import get_container
from velune.models.specializations import ModelSpecializationMapper
from velune.providers.registry import ProviderRegistry


def test_shi_arbitration_thresholds() -> None:
    """Verify that low SHI values scale up required confidence thresholds dynamically."""
    arbitrator = CouncilArbitrator()

    # 1. Without SHI (default threshold 0.55)
    # Deliberation details that passed reviews and have decent logic/confidence scores
    reviewer_report_pass = MagicMock()
    reviewer_report_pass.passed = True
    reviewer_report_pass.confidence_rating = 0.7
    reviewer_report_pass.critical_issues = []

    challenger_report_pass = MagicMock()
    challenger_report_pass.failure_vectors = []
    challenger_report_pass.severity_rating = 0.1

    res_no_shi = arbitrator.arbitrate(
        plan_steps=["Step 1"],
        coder_proposal="print('hello')",
        reviewer_report=reviewer_report_pass,
        challenger_report=challenger_report_pass,
    )
    assert res_no_shi.overall_confidence >= 0.55
    assert res_no_shi.requires_human_review is False

    # 2. With SHI (threshold scaled up to 0.65 or 0.75)
    # We simulate a slightly less confident deliberation that would pass standard 0.55,
    # but gets blocked by elevated thresholds.
    reviewer_report_fail = MagicMock()
    reviewer_report_fail.passed = False
    reviewer_report_fail.confidence_rating = 0.5
    reviewer_report_fail.critical_issues = ["Minor warning"]

    challenger_report_fail = MagicMock()
    challenger_report_fail.failure_vectors = ["Edge case failure"]
    challenger_report_fail.severity_rating = 0.4

    # Confidence rating will be ~0.61
    res_mod_shi = arbitrator.arbitrate(
        plan_steps=["Step 1"],
        coder_proposal="print('hello')",
        reviewer_report=reviewer_report_fail,
        challenger_report=challenger_report_fail,
        shi=0.70,  # elevates threshold to 0.65
    )
    assert res_mod_shi.overall_confidence < 0.65
    assert res_mod_shi.requires_human_review is True


def test_fan_in_tier_escalation() -> None:
    """Verify that high fan-in files escalate the council tier floor safely."""
    # Build a simulated repository graph and registry
    container = get_container()

    # Backup original registration
    has_cognition = container.has("runtime.repository_cognition")
    original_cognition = container.get("runtime.repository_cognition") if has_cognition else None

    try:
        repo_service = MagicMock()
        grapher = MagicMock()
        repo_service.grapher = grapher

        container.register_instance("runtime.repository_cognition", repo_service)

        # Mock dependents
        # target_file: has 6 dependents (escalates to CouncilTier.FULL)
        # target_file_mod: has 4 dependents (escalates to CouncilTier.STANDARD)
        # target_file_low: has 2 dependents (escalates to CouncilTier.MINIMAL)
        # target_file_none: has 0 dependents (no escalation floor - INSTANT fallback)
        def mock_dependents(file_path: str) -> list[str]:
            if "kernel" in file_path or "core" in file_path:
                return ["dep1.py", "dep2.py", "dep3.py", "dep4.py", "dep5.py", "dep6.py"]
            if "standard" in file_path:
                return ["dep1.py", "dep2.py", "dep3.py", "dep4.py"]
            if "minimal" in file_path:
                return ["dep1.py", "dep2.py"]
            return []

        grapher.get_dependents.side_effect = mock_dependents

        # Create Classifier
        classifier = TierClassifier(max_council_tier="full", default_tier_override="auto")

        # 1. High fan-in target must escalate to FULL tier
        tier1 = classifier.classify("Modify velune/kernel/bus.py", "Context")
        assert tier1 == CouncilTier.FULL

        # 2. Moderate fan-in target must escalate to STANDARD tier
        tier2 = classifier.classify("Modify velune/standard/auth.py", "Context")
        assert tier2 == CouncilTier.STANDARD

        # 3. Low fan-in target must escalate to MINIMAL tier
        # However, a keyword like "explain" chooses INSTANT, but the floor raises it to MINIMAL!
        tier3 = classifier.classify("Explain velune/minimal/helper.py", "Context")
        assert tier3 == CouncilTier.MINIMAL

        # 4. No dependents target uses keyword classification directly (INSTANT)
        tier4 = classifier.classify("Explain velune/other/script.py", "Context")
        assert tier4 == CouncilTier.INSTANT

        # 5. Enforce Max Council Tier ceiling
        # Even if fan-in is high, if ceiling is STANDARD, it must NOT exceed STANDARD!
        classifier.max_council_tier = "standard"
        tier5 = classifier.classify("Modify velune/kernel/bus.py", "Context")
        assert tier5 == CouncilTier.STANDARD

        # 6. Enforce Low Resource Mode
        # In low resource mode, FULL upgrades map to STANDARD
        classifier.max_council_tier = "full"
        classifier.low_resource_mode = True
        tier6 = classifier.classify("Modify velune/kernel/bus.py", "Context")
        assert tier6 == CouncilTier.STANDARD

    finally:
        # Restore container registration
        container.clear()
        if has_cognition and original_cognition is not None:
            container.register_instance("runtime.repository_cognition", original_cognition)


def test_lightweight_blast_radius_computation() -> None:
    """Verify estimate_blast_radius executes attenuation-weighted estimations correctly."""
    # Build a simulated repository graph and registry
    container = get_container()

    # Backup original registration
    has_cognition = container.has("runtime.repository_cognition")
    original_cognition = container.get("runtime.repository_cognition") if has_cognition else None

    try:
        repo_service = MagicMock()
        grapher = MagicMock()
        repo_service.grapher = grapher

        container.register_instance("runtime.repository_cognition", repo_service)

        # Mock relative path converter
        grapher._to_rel_path.side_effect = lambda x: x

        # Create mock orchestrator
        providers = MagicMock(spec=ProviderRegistry)
        mapper = MagicMock(spec=ModelSpecializationMapper)

        with tempfile.TemporaryDirectory() as temp_dir:
            orchestrator = CouncilOrchestrator(
                provider_registry=providers,
                mapper=mapper,
                lineage_db_path=Path(temp_dir) / "test.db",
            )

            # 1. Target not in graph must return default
            grapher.graph = {}
            score1 = orchestrator.estimate_blast_radius("nonexistent.py")
            assert score1 == 0.3

            # 2. Mock graph structure
            # target has direct dependents: d1_a, d1_b
            # d1_a has dependents: d2_a
            # d1_b has dependents: d2_b, d2_c
            grapher.graph = {"target.py": {}}

            def mock_get_dependents(file_path: str) -> list[str]:
                if file_path == "target.py":
                    return ["d1_a.py", "d1_b.py"]
                if file_path == "d1_a.py":
                    return ["d2_a.py"]
                if file_path == "d1_b.py":
                    return ["d2_b.py", "d2_c.py"]
                return []

            grapher.get_dependents.side_effect = mock_get_dependents

            # Calculate raw_score = 1.0 * len(d1) + 0.5 * len(d2)
            # len(d1) = 2, len(d2) = 3 (d2_a, d2_b, d2_c)
            # raw_score = 2 * 1.0 + 3 * 0.5 = 3.5
            # normalized = 0.1 + 0.8 * (1.0 - exp(-3.5 / 5.0)) ~= 0.502
            score2 = orchestrator.estimate_blast_radius("target.py")
            assert 0.45 < score2 < 0.55

    finally:
        # Restore container registration
        container.clear()
        if has_cognition and original_cognition is not None:
            container.register_instance("runtime.repository_cognition", original_cognition)


def test_advanced_structural_change_detection() -> None:
    """Verify that core files and high fan-in targets are detected as structural changes."""
    # Build a simulated repository graph and registry
    container = get_container()

    # Backup original registration
    has_cognition = container.has("runtime.repository_cognition")
    original_cognition = container.get("runtime.repository_cognition") if has_cognition else None

    try:
        repo_service = MagicMock()
        grapher = MagicMock()
        repo_service.grapher = grapher

        container.register_instance("runtime.repository_cognition", repo_service)

        # Create mock orchestrator
        providers = MagicMock(spec=ProviderRegistry)
        mapper = MagicMock(spec=ModelSpecializationMapper)

        with tempfile.TemporaryDirectory() as temp_dir:
            orchestrator = CouncilOrchestrator(
                provider_registry=providers,
                mapper=mapper,
                lineage_db_path=Path(temp_dir) / "test.db",
            )

            # Define dependents rule: high_coupled has 4 dependents, low has 0.
            def mock_get_dependents(file_path: str) -> list[str]:
                if "high_coupled" in file_path:
                    return ["dep1.py", "dep2.py", "dep3.py", "dep4.py"]
                return []

            grapher.get_dependents.side_effect = mock_get_dependents

            # 1. Simple change on low coupling target (False)
            assert orchestrator._is_structural_change("Fix typo in helper.py", "context") is False

            # 2. Target Core match must be structural (True)
            assert (
                orchestrator._is_structural_change("Update velune/core/main.py", "context") is True
            )
            assert (
                orchestrator._is_structural_change("Tweak velune/kernel/bus.py", "context") is True
            )

            # 3. High fan-in file match must be structural (True)
            assert orchestrator._is_structural_change("Edit high_coupled.py", "context") is True

            # 4. Keyword structural indicators fallback (True)
            assert (
                orchestrator._is_structural_change("Redesign the state routing", "context") is True
            )

    finally:
        # Restore container registration
        container.clear()
        if has_cognition and original_cognition is not None:
            container.register_instance("runtime.repository_cognition", original_cognition)


@pytest.mark.asyncio
async def test_orchestration_continuity_logging() -> None:
    """Verify that calculated structural impact is logged to the DLS SQLite tables."""
    from velune.memory.storage.sqlite_pool import SQLiteConnectionPool
    from velune.memory.tiers.lineage import LineageMemoryTier

    providers = MagicMock(spec=ProviderRegistry)
    mapper = MagicMock(spec=ModelSpecializationMapper)

    with tempfile.TemporaryDirectory() as temp_dir:
        pool = SQLiteConnectionPool(Path(temp_dir) / "test_dls.db")
        await pool.startup()
        lineage = LineageMemoryTier(pool)
        await lineage.initialize()

        orchestrator = CouncilOrchestrator(
            provider_registry=providers,
            mapper=mapper,
            lineage_memory=lineage,
        )

        # Mock estimate_blast_radius
        orchestrator.estimate_blast_radius = MagicMock(return_value=0.555)

        # Log standard path decision
        decision_id = "DEC-TEST-01"
        await orchestrator.lineage_memory.log_decision(
            decision_id=decision_id,
            target_subsystem="velune/core/main.py",
            rationale="Testing dynamic DLS impact logging",
            architectural_impact=orchestrator.estimate_blast_radius("velune/core/main.py"),
            consequences="Impact score should be exactly 0.555",
        )

        decisions = await orchestrator.lineage_memory.get_subsystem_decisions("main.py")
        assert len(decisions) == 1
        assert decisions[0]["id"] == decision_id
        assert decisions[0]["architectural_impact"] == 0.555

        await pool.shutdown()
