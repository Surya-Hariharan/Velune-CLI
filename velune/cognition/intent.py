"""Intent reconstruction module for the Velune cognition layer.

Classifies user input into a typed ``IntentType`` without making any LLM calls.
Uses keyword matching and structural signals, keeping latency near zero so
it can run on every REPL turn.
"""

from __future__ import annotations

import re

from velune._compat import StrEnum

_WORD_BOUNDARY = re.compile(r"\b")  # used in _score word-boundary checks


class IntentType(StrEnum):
    """Canonical intent categories recognised by Velune's orchestration layer."""

    EXPLAIN = "explain"
    GENERATE = "generate"
    REFACTOR = "refactor"
    DEBUG = "debug"
    REVIEW = "review"
    QUESTION = "question"
    COMMAND = "command"


# ── Keyword signal tables ─────────────────────────────────────────────────────

_COMMAND_SIGNALS: frozenset[str] = frozenset(
    {
        "/",
        "velune",
        "run ",
        "execute ",
        "install ",
        "uninstall ",
        "configure ",
        "set ",
        "enable ",
        "disable ",
        "start ",
        "stop ",
    }
)

_DEBUG_KEYWORDS: frozenset[str] = frozenset(
    {
        "fix",
        "broken",
        "bug",
        "error",
        "exception",
        "traceback",
        "crash",
        "fail",
        "failing",
        "not working",
        "doesnt work",
        "doesn't work",
        "wrong output",
        "unexpected",
        "why is",
        "why does",
        "why did",
    }
)

_EXPLAIN_KEYWORDS: frozenset[str] = frozenset(
    {
        "what",
        "what is",
        "what does",
        "what are",
        "how does",
        "how do",
        "explain",
        "describe",
        "show me",
        "walk me through",
        "tell me",
        "help me understand",
        "what's the",
    }
)

_GENERATE_KEYWORDS: frozenset[str] = frozenset(
    {
        "write",
        "create",
        "generate",
        "add",
        "implement",
        "build",
        "make",
        "scaffold",
        "bootstrap",
        "new function",
        "new class",
        "new file",
        "add a",
        "add an",
    }
)

_REFACTOR_KEYWORDS: frozenset[str] = frozenset(
    {
        "refactor",
        "clean",
        "improve",
        "optimise",
        "optimize",
        "simplify",
        "restructure",
        "reorganize",
        "reorganise",
        "rename",
        "move",
        "extract",
        "consolidate",
        "deduplicate",
        "dedup",
    }
)

_REVIEW_KEYWORDS: frozenset[str] = frozenset(
    {
        "review",
        "check",
        "verify",
        "audit",
        "look at",
        "inspect",
        "analyse",
        "analyze",
        "is this correct",
        "does this look right",
        "any issues",
        "any problems",
    }
)

# Regex: error stack trace signature
_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent|File \".+\", line \d+|Error:|Exception:|error:|exception:)",
    re.IGNORECASE,
)


class IntentClassifier:
    """Zero-latency intent classifier using keyword and structural heuristics."""

    def classify(self, text: str) -> IntentType:
        """Classify *text* into the most likely ``IntentType``.

        Runs in O(N) where N is the number of tokens in *text*.
        Makes no external calls; deterministic output.
        """
        return self.classify_with_confidence(text)[0]

    def classify_with_confidence(self, text: str) -> tuple[IntentType, float]:
        """Return ``(IntentType, confidence)`` for *text*.

        Confidence is a rough 0.0–1.0 score based on signal strength.
        """
        if not text or not text.strip():
            return IntentType.QUESTION, 0.0

        # Slash commands or meta-operations
        stripped = text.strip()
        if stripped.startswith("/"):
            return IntentType.COMMAND, 1.0

        lower = stripped.lower()

        # Stack trace / error log → DEBUG
        if _TRACEBACK_RE.search(text):
            return IntentType.DEBUG, 0.95

        # Count keyword hits per category
        scores: dict[IntentType, float] = {
            IntentType.COMMAND: self._score(lower, _COMMAND_SIGNALS, exact_prefix=True),
            # Debug signals like "error" appear inside compound words (KeyError, ValueError)
            # so use substring matching rather than word boundaries.
            IntentType.DEBUG: self._score(lower, _DEBUG_KEYWORDS, word_boundary=False),
            IntentType.EXPLAIN: self._score(lower, _EXPLAIN_KEYWORDS, exact_prefix=True),
            IntentType.GENERATE: self._score(lower, _GENERATE_KEYWORDS),
            IntentType.REFACTOR: self._score(lower, _REFACTOR_KEYWORDS),
            IntentType.REVIEW: self._score(lower, _REVIEW_KEYWORDS),
        }

        best_intent = max(scores, key=lambda k: scores[k])
        best_score = scores[best_intent]

        if best_score < 0.1:
            return IntentType.QUESTION, 0.5

        return best_intent, min(best_score, 1.0)

    @staticmethod
    def _score(
        text: str,
        signals: frozenset[str],
        exact_prefix: bool = False,
        word_boundary: bool = True,
    ) -> float:
        """Score *text* against a set of keyword signals.

        When *exact_prefix* is True, checks whether the text starts with any
        signal.  When *word_boundary* is True (default), uses ``\\b`` anchors so
        "build" does not fire on "rebuild".  Pass ``word_boundary=False`` for
        signals that commonly appear inside compound words (e.g. "error" inside
        "KeyError").
        """
        hits = 0
        if exact_prefix:
            for sig in signals:
                if text.startswith(sig.lower()) or f" {sig.lower()}" in text:
                    hits += 1
        elif word_boundary:
            for sig in signals:
                pattern = r"\b" + re.escape(sig.lower()) + r"\b"
                if re.search(pattern, text):
                    hits += 1
        else:
            for sig in signals:
                if sig.lower() in text:
                    hits += 1

        # Normalise: cap at 3 hits = full confidence
        return min(hits / 3.0, 1.0)
