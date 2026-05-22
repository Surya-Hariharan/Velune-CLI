"""Intent Hypothesis schemas and builders."""

from __future__ import annotations

from typing import Any, Dict, List
from pydantic import BaseModel, Field


class IntentHypothesis(BaseModel):
    """A single candidate goal interpretation of the user's intent."""
    goal_description: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    primary_category: str = "general"  # coding, debugging, planning, etc.
    target_files: List[str] = Field(default_factory=list)
    action_plan: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HypothesisGenerator:
    """Heuristic hypothesis builder for intent reconstruction."""

    def __init__(self) -> None:
        pass

    def generate_candidates(self, signals: Dict[str, Any], temporal_offset: Optional[float]) -> List[IntentHypothesis]:
        """
        Formulate candidate goal paths based on linguistic parsed signals.
        """
        candidates: List[IntentHypothesis] = []
        raw_text = signals.get("raw_text", "")

        # Default fallback general hypothesis
        default_candidate = IntentHypothesis(
            goal_description=f"Execute user request: {raw_text}",
            confidence=0.4,
            primary_category="general",
            target_files=signals.get("target_files", []),
        )

        # 1. Detect Coding Intent
        coding_verbs = {"create", "make", "build", "generate", "add"}
        has_coding_verb = any(v in signals.get("action_verbs", []) for v in coding_verbs)
        
        if has_coding_verb or signals.get("target_files"):
            confidence = 0.8 if has_coding_verb else 0.6
            candidates.append(IntentHypothesis(
                goal_description=f"Generate or modify codebase files based on: {raw_text}",
                confidence=confidence,
                primary_category="coding",
                target_files=signals.get("target_files", []),
                action_plan=[
                    "Analyze repository AST files",
                    "Formulate target diff modifications",
                    "Apply changes in subprocess sandbox",
                    "Validate compilation & test suites",
                ]
            ))

        # 2. Detect Debugging Intent
        debug_verbs = {"fix", "debug", "resolve", "repair", "patch"}
        has_debug_verb = any(v in signals.get("action_verbs", []) for v in debug_verbs)
        
        if has_debug_verb:
            candidates.append(IntentHypothesis(
                goal_description=f"Debug and resolve code exceptions or failures in: {raw_text}",
                confidence=0.85,
                primary_category="debugging",
                target_files=signals.get("target_files", []),
                action_plan=[
                    "Scan file structure and traceback logs",
                    "Synthesize patch implementation",
                    "Run validation test assertions",
                ]
            ))

        # Add fallback last to act as low-confidence catchall
        candidates.append(default_candidate)
        
        # Sort candidates by confidence
        return sorted(candidates, key=lambda x: x.confidence, reverse=True)
