"""Unit tests for the confidence-scored claim schema (Claim/ClaimAccumulator)
and its wiring into TechnologyDetector.

``docs/repository-intelligence-baseline.md`` §8 (Weakness 1): "Classification
failures are indistinguishable from absence of the thing being classified...
There is no confidence score or 'detection inconclusive' signal anywhere in
this pipeline." Recommendation 4/5: replace binary present/absent
classification with a confidence score, and make tech_stack fields
genuinely multi-valued instead of a single overwritten scalar.

Claim/ClaimAccumulator (schemas.py) are additive: TechStack.language/
.framework scalars are unchanged for backward compatibility, but
.language_claims/.framework_claims now carry every signal's contribution,
ranked by confidence.
"""

from __future__ import annotations

from velune.repository.schemas import Claim, ClaimAccumulator
from velune.repository.technology_detector import TechnologyDetector


class TestClaimAccumulator:
    def test_single_signal_produces_one_claim(self):
        acc = ClaimAccumulator()
        acc.add("Python", 0.5, "pyproject.toml present")
        claims = acc.claims()
        assert len(claims) == 1
        assert claims[0].value == "Python"
        assert claims[0].confidence == 0.5
        assert claims[0].source_signals == ["pyproject.toml present"]

    def test_agreeing_signals_raise_confidence_additively(self):
        acc = ClaimAccumulator()
        acc.add("Python", 0.5, "requirements.txt present")
        acc.add("Python", 0.3, "pyproject.toml present")
        claims = acc.claims()
        assert len(claims) == 1
        assert claims[0].confidence == 0.8
        assert set(claims[0].source_signals) == {"requirements.txt present", "pyproject.toml present"}

    def test_confidence_is_capped_at_one(self):
        acc = ClaimAccumulator()
        acc.add("Python", 0.7, "signal a")
        acc.add("Python", 0.7, "signal b")
        assert acc.claims()[0].confidence == 1.0

    def test_disagreeing_signals_produce_separate_ranked_claims(self):
        acc = ClaimAccumulator()
        acc.add("Python", 0.5, "pyproject.toml present")
        acc.add("Rust", 0.75, "Cargo.toml present")
        claims = acc.claims()
        assert [c.value for c in claims] == ["Rust", "Python"]  # highest confidence first

    def test_best_returns_highest_confidence_value(self):
        acc = ClaimAccumulator()
        acc.add("Python", 0.5, "x")
        acc.add("Rust", 0.75, "y")
        assert acc.best() == "Rust"

    def test_best_returns_none_when_nothing_recorded(self):
        assert ClaimAccumulator().best() is None

    def test_falsy_value_is_a_no_op(self):
        acc = ClaimAccumulator()
        acc.add("", 0.9, "x")
        acc.add(None, 0.9, "y")
        assert acc.claims() == []


class TestClaimModel:
    def test_defaults(self):
        claim = Claim(value="Python")
        assert claim.confidence == 1.0
        assert claim.source_signals == []


class TestTechnologyDetectorClaimWiring:
    def test_single_manifest_repo_has_one_language_claim(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
        tech = TechnologyDetector(tmp_path).detect()
        assert [c.value for c in tech.language_claims] == ["Python"]
        assert tech.language == "Python"

    def test_polyglot_repo_has_multiple_language_claims(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
        (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\nversion="0.1.0"\n', encoding="utf-8")
        tech = TechnologyDetector(tmp_path).detect()
        claim_values = {c.value for c in tech.language_claims}
        assert claim_values == {"Python", "Rust"}

    def test_framework_claim_from_explicit_dependency(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\ndependencies=["fastapi"]\n', encoding="utf-8"
        )
        tech = TechnologyDetector(tmp_path).detect()
        assert any(c.value == "FastAPI" for c in tech.framework_claims)

    def test_no_manifest_at_all_produces_no_claims(self, tmp_path):
        (tmp_path / "README.md").write_text("hi", encoding="utf-8")
        tech = TechnologyDetector(tmp_path).detect()
        assert tech.language_claims == []
        assert tech.framework_claims == []

    def test_claims_are_serialized_in_to_dict(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
        tech = TechnologyDetector(tmp_path).detect()
        data = tech.to_dict()
        assert "language_claims" in data
        assert data["language_claims"][0]["value"] == "Python"

    def test_detector_instance_is_reusable_across_detect_calls(self, tmp_path):
        """A second .detect() call must not carry over claims from a first
        call on a different (or the same) instance."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
        detector = TechnologyDetector(tmp_path)
        first = detector.detect()
        second = detector.detect()
        assert len(first.language_claims) == len(second.language_claims) == 1
