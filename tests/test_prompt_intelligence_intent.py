"""TaskIntent resolution — see docs/02-prompt-intelligence.md §10.

TaskIntent wraps the existing IntentClassifier rather than adding a second
classifier; these tests pin the many-to-one IntentType -> TaskKind mapping.
"""

from __future__ import annotations

from velune.cognition.intent import IntentType
from velune.prompt_intelligence.intent import TaskIntent, TaskKind, task_kind_from_intent


def test_task_kind_from_intent_covers_every_intent_type():
    for intent_type in IntentType:
        # Must never raise / must always resolve to a valid TaskKind.
        kind = task_kind_from_intent(intent_type)
        assert isinstance(kind, TaskKind)


def test_debug_intent_maps_to_debugging_task_kind():
    assert task_kind_from_intent(IntentType.DEBUG) == TaskKind.DEBUGGING


def test_unmapped_or_ambiguous_intent_falls_back_to_research():
    # QUESTION has no dedicated TaskKind — falls back to RESEARCH (§10).
    assert task_kind_from_intent(IntentType.QUESTION) == TaskKind.RESEARCH


def test_task_intent_from_text_classifies_and_wraps():
    ti = TaskIntent.from_text("fix the crash in the login handler")
    assert ti.kind == TaskKind.DEBUGGING
    assert ti.intent_type == IntentType.DEBUG
    assert ti.request_text == "fix the crash in the login handler"
    assert 0.0 <= ti.intent_confidence <= 1.0


def test_task_intent_has_no_urgency_field():
    # Deliberate omission — see docs/03-cognitive-architecture.md §0.2:
    # IntentClassifier and the Council's classify_task_tier are independent,
    # uncorrelated classifiers today. TaskIntent must not fabricate a tier.
    ti = TaskIntent.from_text("hello")
    assert not hasattr(ti, "urgency")
    assert not hasattr(ti, "tier")
