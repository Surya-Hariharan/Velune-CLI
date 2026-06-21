"""Tests for velune.cognition.intent — IntentClassifier."""

from __future__ import annotations

import pytest

from velune.cognition.intent import IntentClassifier, IntentType


@pytest.fixture
def clf() -> IntentClassifier:
    return IntentClassifier()


# ── One test per IntentType ───────────────────────────────────────────────────


class TestIntentTypeExplain:
    def test_what_prefix(self, clf: IntentClassifier) -> None:
        assert clf.classify("what does main() do?") == IntentType.EXPLAIN

    def test_how_does(self, clf: IntentClassifier) -> None:
        assert clf.classify("how does the retrieval pipeline work?") == IntentType.EXPLAIN

    def test_explain_keyword(self, clf: IntentClassifier) -> None:
        assert clf.classify("explain the memory lifecycle") == IntentType.EXPLAIN


class TestIntentTypeGenerate:
    def test_write_function(self, clf: IntentClassifier) -> None:
        assert clf.classify("write a FastAPI endpoint for user registration") == IntentType.GENERATE

    def test_create_class(self, clf: IntentClassifier) -> None:
        assert clf.classify("create a TokenBucket class with rate limiting") == IntentType.GENERATE

    def test_implement(self, clf: IntentClassifier) -> None:
        assert clf.classify("implement a retry decorator") == IntentType.GENERATE

    def test_add_feature(self, clf: IntentClassifier) -> None:
        assert clf.classify("add a new CLI command for listing sessions") == IntentType.GENERATE


class TestIntentTypeRefactor:
    def test_refactor_keyword(self, clf: IntentClassifier) -> None:
        assert clf.classify("refactor this function to be async") == IntentType.REFACTOR

    def test_simplify(self, clf: IntentClassifier) -> None:
        assert clf.classify("simplify the context assembly logic") == IntentType.REFACTOR

    def test_optimize(self, clf: IntentClassifier) -> None:
        assert clf.classify("optimize the BM25 index rebuild") == IntentType.REFACTOR


class TestIntentTypeDebug:
    def test_fix_keyword(self, clf: IntentClassifier) -> None:
        assert clf.classify("fix the broken import in memory/lifecycle.py") == IntentType.DEBUG

    def test_error_keyword(self, clf: IntentClassifier) -> None:
        assert clf.classify("I'm getting a KeyError in the provider registry") == IntentType.DEBUG

    def test_traceback_signal(self, clf: IntentClassifier) -> None:
        text = (
            "Traceback (most recent call last):\n"
            "  File 'velune/cognition/coder.py', line 42, in _parse_diffs\n"
            "AttributeError: 'NoneType' has no attribute 'file_path'"
        )
        assert clf.classify(text) == IntentType.DEBUG

    def test_not_working(self, clf: IntentClassifier) -> None:
        assert clf.classify("the session persistence is not working") == IntentType.DEBUG


class TestIntentTypeReview:
    def test_review_keyword(self, clf: IntentClassifier) -> None:
        assert clf.classify("review this pull request") == IntentType.REVIEW

    def test_check_code(self, clf: IntentClassifier) -> None:
        assert clf.classify("check this code for security issues") == IntentType.REVIEW

    def test_audit(self, clf: IntentClassifier) -> None:
        assert clf.classify("audit the plugin sandbox implementation") == IntentType.REVIEW


class TestIntentTypeCommand:
    def test_slash_prefix(self, clf: IntentClassifier) -> None:
        assert clf.classify("/help") == IntentType.COMMAND

    def test_slash_with_args(self, clf: IntentClassifier) -> None:
        assert clf.classify("/memory list") == IntentType.COMMAND

    def test_configure(self, clf: IntentClassifier) -> None:
        assert clf.classify("configure the anthropic API key") == IntentType.COMMAND


class TestIntentTypeQuestion:
    def test_fallback_for_ambiguous(self, clf: IntentClassifier) -> None:
        assert clf.classify("I want to learn about embeddings") == IntentType.QUESTION

    def test_empty_string(self, clf: IntentClassifier) -> None:
        result, conf = clf.classify_with_confidence("")
        assert result == IntentType.QUESTION
        assert conf == 0.0


# ── Confidence tests ──────────────────────────────────────────────────────────


class TestConfidenceScores:
    def test_slash_is_certain(self, clf: IntentClassifier) -> None:
        _, conf = clf.classify_with_confidence("/session save")
        assert conf == 1.0

    def test_traceback_is_high_confidence(self, clf: IntentClassifier) -> None:
        _, conf = clf.classify_with_confidence("Traceback (most recent call last): ...")
        assert conf >= 0.9

    def test_ambiguous_has_lower_confidence(self, clf: IntentClassifier) -> None:
        _, conf = clf.classify_with_confidence("something")
        assert conf <= 0.5


# ── Classify with existing intent ────────────────────────────────────────────


class TestEngineClassifyIntent:
    """Test IntentClassifier used via ContextOrchestrationEngine.classify_intent()."""

    def test_engine_classify_fresh(self) -> None:
        from velune.orchestration.engine import ContextOrchestrationEngine

        engine = ContextOrchestrationEngine()
        intent = engine.classify_intent("write a new module")
        assert intent == IntentType.GENERATE

    def test_engine_respects_existing_intent(self) -> None:
        from velune.orchestration.engine import ContextOrchestrationEngine

        engine = ContextOrchestrationEngine()
        intent = engine.classify_intent("anything", existing_intent="review")
        assert intent == IntentType.REVIEW

    def test_engine_ignores_invalid_existing_intent(self) -> None:
        from velune.orchestration.engine import ContextOrchestrationEngine

        engine = ContextOrchestrationEngine()
        intent = engine.classify_intent("explain this", existing_intent="nonsense")
        assert intent == IntentType.EXPLAIN
