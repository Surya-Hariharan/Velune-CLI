"""Context Stitcher for prompt composition."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("velune.context.stitcher")


class ContextStitcher:
    """Combines diverse context categories into a final, structured prompt context string."""

    def __init__(self) -> None:
        pass

    def stitch(
        self,
        working_turns: List[Dict[str, Any]],
        episodic_steps: List[Dict[str, Any]],
        semantic_chunks: List[Dict[str, Any]],
        repository_ast: Optional[str] = None,
        git_diffs: Optional[str] = None,
    ) -> str:
        """
        Stitch together all components using standard XML-like delimiters for structural clarity.
        """
        parts = []

        # 1. System/Global Context Boundaries
        parts.append("=== COGNITIVE SYSTEM CONTEXT BOUNDARY ===")

        # 2. Add Repository Structure & AST Details
        if repository_ast:
            parts.append("\n<repository_ast_structure>")
            parts.append(repository_ast)
            parts.append("</repository_ast_structure>")

        # 3. Add Git Diff details
        if git_diffs:
            parts.append("\n<active_git_changes>")
            parts.append(git_diffs)
            parts.append("</active_git_changes>")

        # 4. Add Semantic Chunks and Facts
        if semantic_chunks:
            parts.append("\n<relevant_semantic_knowledge>")
            for i, chunk in enumerate(semantic_chunks):
                payload = chunk.get("payload", {})
                fact = payload.get("fact", "")
                if fact:
                    parts.append(f"Fact {i+1}: {fact}")
                code_snippet = payload.get("code", "")
                if code_snippet:
                    parts.append(f"File: {payload.get('file_path', 'unknown')}\nCode:\n{code_snippet}")
            parts.append("</relevant_semantic_knowledge>")

        # 5. Add Episodic steps/execution logs
        if episodic_steps:
            parts.append("\n<historical_execution_trace>")
            for i, step in enumerate(episodic_steps):
                step_name = step.get("step_name", "step")
                status = step.get("status", "unknown")
                parts.append(f"Step {i+1}: [{step_name}] Status: {status}")
            parts.append("</historical_execution_trace>")

        # 6. Conversation history
        if working_turns:
            parts.append("\n<conversation_history>")
            for turn in working_turns:
                role = turn.get("role", "user").upper()
                content = turn.get("content", "")
                parts.append(f"{role}: {content}")
            parts.append("</conversation_history>")

        parts.append("\n=== END COGNITIVE SYSTEM CONTEXT ===")

        return "\n".join(parts)
