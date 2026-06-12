"""Tests for Phase 2a blast radius estimator.

Tests verify:
1. Structural importance (fan-in counts)
2. Boundary criticality (auth/API/DB/payment boundaries)
3. Combined scoring formula
4. File ranking by blast radius
"""

from __future__ import annotations

import pytest

from velune.repository.blast_radius import BlastRadiusEstimator
from velune.repository.boundary_classifier import BoundaryClassifier, BoundaryType
from velune.repository.import_graph import ImportGraphBuilder


class TestImportGraphBuilder:
    """Test import graph construction."""

    @pytest.fixture
    def builder(self) -> ImportGraphBuilder:
        return ImportGraphBuilder()

    def test_discover_files_empty_graph(self, builder: ImportGraphBuilder) -> None:
        graph = builder.get_all_metrics()
        assert isinstance(graph, dict)

    def test_add_import_edge(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("auth.py", "utils.py")
        metrics = builder.get_metrics("auth.py")
        assert metrics is not None
        assert "utils.py" in metrics.imports

    def test_self_import_ignored(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("utils.py", "utils.py")
        metrics = builder.get_metrics("utils.py")
        if metrics is not None:
            assert "utils.py" not in metrics.imports

    def test_fan_in_computation(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("auth.py", "utils.py")
        builder._add_import_edge("api.py", "utils.py")
        builder._add_import_edge("models.py", "utils.py")
        builder._compute_metrics()

        utils_metrics = builder.get_metrics("utils.py")
        assert utils_metrics is not None
        assert utils_metrics.fan_in == 3

    def test_fan_out_computation(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("auth.py", "utils.py")
        builder._add_import_edge("auth.py", "models.py")
        builder._add_import_edge("auth.py", "db.py")
        builder._compute_metrics()

        auth_metrics = builder.get_metrics("auth.py")
        assert auth_metrics is not None
        assert auth_metrics.fan_out == 3

    def test_is_imported_by_tests(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("test_utils.py", "utils.py")
        builder._add_import_edge("utils.test.ts", "utils.py")
        builder._compute_metrics()

        utils_metrics = builder.get_metrics("utils.py")
        assert utils_metrics is not None
        assert utils_metrics.is_imported_by_tests is True

    def test_is_not_imported_by_tests(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("auth.py", "utils.py")
        builder._compute_metrics()

        utils_metrics = builder.get_metrics("utils.py")
        assert utils_metrics is not None
        assert utils_metrics.is_imported_by_tests is False

    def test_get_importers(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("auth.py", "utils.py")
        builder._add_import_edge("api.py", "utils.py")

        importers = builder.get_importers("utils.py")
        assert importers == {"auth.py", "api.py"}

    def test_get_imports(self, builder: ImportGraphBuilder) -> None:
        builder._add_import_edge("auth.py", "utils.py")
        builder._add_import_edge("auth.py", "models.py")

        imports = builder.get_imports("auth.py")
        assert imports == {"utils.py", "models.py"}

    def test_transitive_dependents(self, builder: ImportGraphBuilder) -> None:
        # Create a chain: a -> b -> c
        builder._add_import_edge("a.py", "b.py")
        builder._add_import_edge("b.py", "c.py")

        # Transitive dependents of c: a (through b)
        dependents = builder.get_transitive_dependents("c.py")
        assert "a.py" in dependents
        assert "b.py" in dependents


class TestBoundaryClassifier:
    """Test boundary classification."""

    @pytest.fixture
    def classifier(self) -> BoundaryClassifier:
        return BoundaryClassifier()

    def test_classify_authentication(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("auth.py") == BoundaryType.AUTHENTICATION
        assert classifier.classify_by_path_only("auth/login.py") == BoundaryType.AUTHENTICATION
        assert classifier.classify_by_path_only("jwt_handler.py") == BoundaryType.AUTHENTICATION
        assert classifier.classify_by_path_only("oauth.ts") == BoundaryType.AUTHENTICATION

    def test_classify_api_surface(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("routes.py") == BoundaryType.API_SURFACE
        assert classifier.classify_by_path_only("api/endpoints.py") == BoundaryType.API_SURFACE
        assert classifier.classify_by_path_only("controllers.ts") == BoundaryType.API_SURFACE
        assert classifier.classify_by_path_only("handlers.js") == BoundaryType.API_SURFACE

    def test_classify_database(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("models.py") == BoundaryType.DATABASE
        assert classifier.classify_by_path_only("db/schema.py") == BoundaryType.DATABASE
        assert classifier.classify_by_path_only("migrations.py") == BoundaryType.DATABASE
        assert classifier.classify_by_path_only("repository.ts") == BoundaryType.DATABASE

    def test_classify_payment(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("payment.py") == BoundaryType.PAYMENT
        assert classifier.classify_by_path_only("stripe.py") == BoundaryType.PAYMENT
        assert classifier.classify_by_path_only("billing/invoices.ts") == BoundaryType.PAYMENT

    def test_classify_event_system(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("events.py") == BoundaryType.EVENT_SYSTEM
        assert classifier.classify_by_path_only("pubsub.ts") == BoundaryType.EVENT_SYSTEM
        assert classifier.classify_by_path_only("kafka_producer.py") == BoundaryType.EVENT_SYSTEM

    def test_classify_non_boundary(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("utils.py") is None
        assert classifier.classify_by_path_only("helpers/string_utils.ts") is None

    def test_classify_case_insensitive(self, classifier: BoundaryClassifier) -> None:
        assert classifier.classify_by_path_only("AUTH.PY") == BoundaryType.AUTHENTICATION
        assert classifier.classify_by_path_only("API/ROUTES.TS") == BoundaryType.API_SURFACE

    def test_batch_classify(self, classifier: BoundaryClassifier) -> None:
        files = ["auth.py", "routes.py", "models.py", "utils.py"]
        result = classifier.batch_classify(files)

        assert result["auth.py"] == BoundaryType.AUTHENTICATION
        assert result["routes.py"] == BoundaryType.API_SURFACE
        assert result["models.py"] == BoundaryType.DATABASE
        assert result["utils.py"] is None

    def test_get_all_boundaries_for(self, classifier: BoundaryClassifier) -> None:
        # File that matches multiple patterns
        boundaries = classifier.get_all_boundaries_for("auth_routes.py")
        assert BoundaryType.AUTHENTICATION in boundaries
        assert BoundaryType.API_SURFACE in boundaries


class TestBlastRadiusEstimator:
    """Test blast radius scoring and ranking."""

    @pytest.fixture
    def setup(self) -> tuple[ImportGraphBuilder, BoundaryClassifier, BlastRadiusEstimator]:
        graph = ImportGraphBuilder()
        classifier = BoundaryClassifier()
        estimator = BlastRadiusEstimator(graph, classifier, fan_in_normalization=20)
        return graph, classifier, estimator

    def test_score_high_fan_in(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        # Manually set up high fan-in scenario
        for i in range(15):
            graph._add_import_edge(f"module{i}.py", "central.py")
        graph._compute_metrics()

        score = estimator.compute_score("central.py")
        # fan_in_score = min(1.0, 15/20) = 0.75
        # boundary_score = 0.0 (not a boundary)
        # combined = 0.6 * 0.75 + 0.4 * 0.0 = 0.45
        assert score.fan_in == 15
        assert score.boundary_score == 0.0
        assert abs(score.score - 0.45) < 0.01

    def test_score_boundary_file(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        score = estimator.compute_score("auth.py")
        # fan_in_score = 0.0 (no dependencies)
        # boundary_score = 0.5 (is a boundary)
        # combined = 0.6 * 0.0 + 0.4 * 0.5 = 0.2
        assert score.fan_in == 0
        assert score.boundary_type == BoundaryType.AUTHENTICATION
        assert abs(score.score - 0.2) < 0.01

    def test_score_high_fan_in_and_boundary(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        # High fan-in auth module
        for i in range(25):
            graph._add_import_edge(f"module{i}.py", "auth_core.py")
        graph._compute_metrics()

        score = estimator.compute_score("auth_core.py")
        # fan_in_score = min(1.0, 25/20) = 1.0
        # boundary_score = 0.5 (auth is a boundary)
        # combined = 0.6 * 1.0 + 0.4 * 0.5 = 0.8
        assert score.boundary_type == BoundaryType.AUTHENTICATION
        assert abs(score.score - 0.8) < 0.01

    def test_score_low_fan_in_no_boundary(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        score = estimator.compute_score("utils.py")
        # No dependencies, not a boundary
        # combined = 0.6 * 0.0 + 0.4 * 0.0 = 0.0
        assert score.score == 0.0

    def test_ranking_highest_first(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        # Set up three files with different scores
        for i in range(5):
            graph._add_import_edge(f"a{i}.py", "low_impact.py")
        for i in range(15):
            graph._add_import_edge(f"b{i}.py", "medium_impact.py")
        for i in range(25):
            graph._add_import_edge(f"c{i}.py", "high_impact_utils.py")

        graph._compute_metrics()

        files = ["low_impact.py", "medium_impact.py", "high_impact_utils.py"]
        ranked = estimator.rank_files_by_blast_radius(files)

        # Should be: high, medium, low
        assert ranked[0][0] == "high_impact_utils.py"
        assert ranked[1][0] == "medium_impact.py"
        assert ranked[2][0] == "low_impact.py"

    def test_boundary_breaks_ties(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        # Both have same fan-in, but one is a boundary
        for i in range(10):
            graph._add_import_edge(f"a{i}.py", "normal_utils.py")
            graph._add_import_edge(f"b{i}.py", "api_routes.py")

        graph._compute_metrics()

        score_normal = estimator.compute_score("normal_utils.py")
        score_api = estimator.compute_score("api_routes.py")

        # Both have fan_in=10, but api_routes is a boundary
        assert score_normal.fan_in == score_api.fan_in
        assert score_api.score > score_normal.score

    def test_get_top_n_files(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        for i in range(20):
            graph._add_import_edge(f"a{i}.py", "file1.py")
            graph._add_import_edge(f"b{i}.py", "file2.py")
            graph._add_import_edge(f"c{i}.py", "file3.py")

        graph._compute_metrics()

        files = ["file1.py", "file2.py", "file3.py", "utils.py"]
        top_2 = estimator.get_top_n_files(files, n=2)

        assert len(top_2) == 2
        assert top_2[0].score >= top_2[1].score

    def test_get_high_impact_files(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        for i in range(25):
            graph._add_import_edge(f"a{i}.py", "high_impact.py")

        graph._compute_metrics()

        files = ["high_impact.py", "utils.py"]
        high_impact = estimator.get_high_impact_files(files, threshold=0.4)

        assert len(high_impact) == 1
        assert high_impact[0].file_path == "high_impact.py"

    def test_score_caching(self, setup: tuple) -> None:
        _, _, estimator = setup

        # Compute twice
        score1 = estimator.compute_score("test.py")
        score2 = estimator.compute_score("test.py")

        # Should be the same object (cached)
        assert score1 is score2

    def test_clear_cache(self, setup: tuple) -> None:
        _, _, estimator = setup

        score1 = estimator.compute_score("test.py")
        estimator.clear_cache()
        score2 = estimator.compute_score("test.py")

        # Should be different objects after cache clear
        assert score1 is not score2

    def test_get_score_breakdown(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        for i in range(15):
            graph._add_import_edge(f"mod{i}.py", "core.py")

        graph._compute_metrics()

        breakdown = estimator.get_score_breakdown("core.py")

        assert breakdown["fan_in"] == 15
        assert breakdown["total_score"] > 0
        assert "structural_contribution" in breakdown
        assert "boundary_contribution" in breakdown

    def test_explain_score(self, setup: tuple) -> None:
        graph, classifier, estimator = setup

        for i in range(25):
            graph._add_import_edge(f"mod{i}.py", "auth_core.py")

        graph._compute_metrics()

        explanation = estimator.explain_score("auth_core.py")

        assert "Blast Radius Score" in explanation
        assert "auth_core.py" in explanation
        assert "modules depend on this" in explanation
        assert "boundary" in explanation.lower()


class TestBlastRadiusScoringFormula:
    """Test the specific scoring formula and weights."""

    @pytest.fixture
    def setup(self) -> tuple[ImportGraphBuilder, BoundaryClassifier, BlastRadiusEstimator]:
        graph = ImportGraphBuilder()
        classifier = BoundaryClassifier()
        estimator = BlastRadiusEstimator(graph, classifier, fan_in_normalization=20)
        return graph, classifier, estimator

    def test_formula_weights(self, setup: tuple) -> None:
        """Verify the formula: score = 0.6 * fan_in_score + 0.4 * boundary_score"""
        graph, classifier, estimator = setup

        # Test: fan_in_score = 0.5, boundary_score = 0.0
        for i in range(10):
            graph._add_import_edge(f"mod{i}.py", "file1.py")
        graph._compute_metrics()

        score = estimator.compute_score("file1.py")
        expected = 0.6 * 0.5 + 0.4 * 0.0
        assert abs(score.score - expected) < 0.01

    def test_boundary_bonus_weight(self, setup: tuple) -> None:
        """Verify boundary bonus is 40% of total weight."""
        graph, classifier, estimator = setup

        score = estimator.compute_score("payment.py")
        # fan_in = 0, so fan_in_score = 0.0
        # boundary_score = 0.5 (payment is a boundary)
        # expected = 0.6 * 0.0 + 0.4 * 0.5 = 0.2
        assert abs(score.score - 0.2) < 0.01

    def test_normalization_cap(self, setup: tuple) -> None:
        """Verify fan_in_score caps at 1.0"""
        graph, classifier, estimator = setup

        # Even with very high fan-in, score caps at 1.0
        for i in range(100):
            graph._add_import_edge(f"mod{i}.py", "core.py")
        graph._compute_metrics()

        score = estimator.compute_score("core.py")
        assert score.fan_in_score == 1.0
        assert score.score <= 1.0
