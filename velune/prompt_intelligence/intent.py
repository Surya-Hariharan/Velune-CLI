"""Task intent resolution for prompt compilation.

Wraps the existing zero-latency :class:`~velune.cognition.intent.IntentClassifier`
rather than adding a second classifier — see ``docs/02-prompt-intelligence.md``
§10. ``TaskIntent`` deliberately carries no urgency/tier field: as of this
writing, ``IntentClassifier`` and the Council's ``classify_task_tier`` are
independent, uncorrelated classifiers (``docs/03-cognitive-architecture.md``
§0.2). Add one here once a unified ``TurnUnderstanding`` exists upstream —
reintroducing a second, uncorrelated tier guess in the meantime would just
recreate the problem that document names.
"""

from __future__ import annotations

from dataclasses import dataclass

from velune._compat import StrEnum
from velune.cognition.intent import IntentClassifier, IntentType


class TaskKind(StrEnum):
    """Provider-independent task categories the template registry keys on."""

    CODING = "coding"
    DEBUGGING = "debugging"
    PLANNING = "planning"
    CODE_REVIEW = "code_review"
    REFACTORING = "refactoring"
    DOCUMENTATION = "documentation"
    ARCHITECTURE = "architecture"
    RESEARCH = "research"
    TERMINAL_EXECUTION = "terminal_execution"


# IntentClassifier recognises finer-grained categories than the template
# registry needs distinct templates for — this is a deliberate many-to-one
# map, not an oversight. Unmapped/ambiguous intents fall back to RESEARCH,
# the closest fit for an open-ended "figure this out" turn.
_INTENT_TO_TASK_KIND: dict[IntentType, TaskKind] = {
    IntentType.GENERATE: TaskKind.CODING,
    IntentType.TEST_GENERATION: TaskKind.CODING,
    IntentType.DEBUG: TaskKind.DEBUGGING,
    IntentType.REFACTOR: TaskKind.REFACTORING,
    IntentType.REVIEW: TaskKind.CODE_REVIEW,
    IntentType.SECURITY: TaskKind.CODE_REVIEW,
    IntentType.DOCUMENTATION: TaskKind.DOCUMENTATION,
    IntentType.ARCHITECTURE: TaskKind.ARCHITECTURE,
    IntentType.COMMAND: TaskKind.TERMINAL_EXECUTION,
    IntentType.SEARCH: TaskKind.RESEARCH,
    IntentType.DEPENDENCY_ANALYSIS: TaskKind.RESEARCH,
    IntentType.EXPLAIN: TaskKind.RESEARCH,
    IntentType.QUESTION: TaskKind.RESEARCH,
}


def task_kind_from_intent(intent: IntentType) -> TaskKind:
    """Map a classified ``IntentType`` onto the closest ``TaskKind``."""
    return _INTENT_TO_TASK_KIND.get(intent, TaskKind.RESEARCH)


@dataclass(frozen=True)
class TaskIntent:
    """What the user/system is trying to do, for one turn."""

    kind: TaskKind
    request_text: str
    intent_type: IntentType
    intent_confidence: float

    @classmethod
    def from_text(cls, text: str, classifier: IntentClassifier | None = None) -> TaskIntent:
        """Classify *text* via the existing ``IntentClassifier`` and wrap it."""
        classifier = classifier or IntentClassifier()
        intent_type, confidence = classifier.classify_with_confidence(text)
        return cls(
            kind=task_kind_from_intent(intent_type),
            request_text=text,
            intent_type=intent_type,
            intent_confidence=confidence,
        )
