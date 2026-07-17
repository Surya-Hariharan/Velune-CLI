"""Tests for velune.cognition.intent — IntentClassifier and IntentType.

Covers the original seven intents plus the six added for Phase 4's
Intelligent Retrieval Engine (SEARCH, TEST_GENERATION, SECURITY,
ARCHITECTURE, DOCUMENTATION, DEPENDENCY_ANALYSIS), and the tie-breaking
order that lets the new, more specific phrase categories win over the
older, broader single-word ones (EXPLAIN's "what", GENERATE's "add"/"write")
on an equal score.
"""

from __future__ import annotations

from velune.cognition.intent import IntentClassifier, IntentType


class TestOriginalIntents:
    def test_command_from_slash_prefix(self):
        intent, confidence = IntentClassifier().classify_with_confidence("/index status")
        assert intent == IntentType.COMMAND
        assert confidence == 1.0

    def test_debug_from_traceback(self):
        text = 'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: bad'
        intent, _ = IntentClassifier().classify_with_confidence(text)
        assert intent == IntentType.DEBUG

    def test_debug_from_keyword(self):
        intent, _ = IntentClassifier().classify_with_confidence("fix this crash")
        assert intent == IntentType.DEBUG

    def test_explain_from_question_word(self):
        intent, _ = IntentClassifier().classify_with_confidence("explain what this function does")
        assert intent == IntentType.EXPLAIN

    def test_generate_from_create(self):
        intent, _ = IntentClassifier().classify_with_confidence("create a new endpoint")
        assert intent == IntentType.GENERATE

    def test_refactor_from_keyword(self):
        intent, _ = IntentClassifier().classify_with_confidence("refactor this to be cleaner")
        assert intent == IntentType.REFACTOR

    def test_review_from_keyword(self):
        intent, _ = IntentClassifier().classify_with_confidence("review my changes")
        assert intent == IntentType.REVIEW

    def test_empty_text_is_question_zero_confidence(self):
        intent, confidence = IntentClassifier().classify_with_confidence("")
        assert intent == IntentType.QUESTION
        assert confidence == 0.0

    def test_no_signal_falls_back_to_question(self):
        intent, _ = IntentClassifier().classify_with_confidence(
            "random unrelated statement about nothing keyword-ish"
        )
        assert intent == IntentType.QUESTION


class TestNewIntents:
    """The six categories added for Phase 4 (Part 2)."""

    def test_search(self):
        intent, _ = IntentClassifier().classify_with_confidence(
            "find where the sandbox is validated"
        )
        assert intent == IntentType.SEARCH

    def test_test_generation(self):
        intent, _ = IntentClassifier().classify_with_confidence(
            "write a test for this function"
        )
        assert intent == IntentType.TEST_GENERATION

    def test_security(self):
        intent, confidence = IntentClassifier().classify_with_confidence(
            "is there a sql injection vulnerability here"
        )
        assert intent == IntentType.SECURITY
        assert confidence > 0.5

    def test_architecture(self):
        intent, _ = IntentClassifier().classify_with_confidence(
            "how is this structured, what are the module boundaries"
        )
        assert intent == IntentType.ARCHITECTURE

    def test_documentation(self):
        intent, _ = IntentClassifier().classify_with_confidence(
            "add docstrings to this module"
        )
        assert intent == IntentType.DOCUMENTATION

    def test_documentation_matches_plural_docstrings(self):
        """DOCUMENTATION uses substring matching (word_boundary=False) so the
        singular keyword "docstring" still matches the plural "docstrings" —
        a \\b-anchored match would miss it (\\bdocstring\\b fails inside
        "docstrings", since there's no boundary before the trailing "s")."""
        intent, _ = IntentClassifier().classify_with_confidence("write docstrings please")
        assert intent == IntentType.DOCUMENTATION

    def test_dependency_analysis(self):
        intent, _ = IntentClassifier().classify_with_confidence("what depends on this file")
        assert intent == IntentType.DEPENDENCY_ANALYSIS

    def test_all_intent_types_are_reachable(self):
        """Every IntentType member should be producible by at least one input —
        a category nobody's keywords can ever select is dead code."""
        classifier = IntentClassifier()
        probes = {
            IntentType.EXPLAIN: "explain what this does",
            IntentType.GENERATE: "create a new file",
            IntentType.REFACTOR: "refactor this function",
            IntentType.DEBUG: "fix this bug",
            IntentType.REVIEW: "review my changes",
            IntentType.QUESTION: "zzz nonsense zzz",
            IntentType.COMMAND: "/help",
            IntentType.SEARCH: "find the config loader",
            IntentType.TEST_GENERATION: "generate tests for this class",
            IntentType.SECURITY: "check for xss vulnerabilities",
            IntentType.ARCHITECTURE: "describe the system design and layering",
            IntentType.DOCUMENTATION: "add docs for this api",
            IntentType.DEPENDENCY_ANALYSIS: "check the blast radius before merging this",
        }
        for expected, text in probes.items():
            intent, _ = classifier.classify_with_confidence(text)
            assert intent == expected, f"{text!r} classified as {intent}, expected {expected}"
