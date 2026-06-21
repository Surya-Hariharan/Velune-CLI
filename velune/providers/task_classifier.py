"""Task classification based on prompt content and context."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskType(StrEnum):
    """Task type categories for routing."""

    CODING = "coding"
    REASONING = "reasoning"
    SUMMARIZATION = "summarization"
    GENERAL = "general"
    EMBEDDING = "embedding"


class ComplexityLevel(StrEnum):
    """Task complexity estimation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class TaskProfile:
    """Profile of a task for routing decisions."""

    task_type: TaskType
    complexity: ComplexityLevel
    latency_sensitive: bool
    requires_long_context: bool
    requires_tools: bool
    estimated_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskClassifier:
    """Classifies prompts and context into task profiles for routing."""

    # Coding-related keywords
    CODING_KEYWORDS = {
        "implement",
        "write",
        "code",
        "function",
        "class",
        "method",
        "fix",
        "bug",
        "error",
        "debug",
        "refactor",
        "rewrite",
        "modify",
        "change",
        "update",
        "add",
        "remove",
        "delete",
        "script",
        "program",
        "algorithm",
        "library",
        "framework",
        "test",
        "unittest",
        "pytest",
        "jest",
        "mocha",
        "sql",
        "query",
        "database",
        "schema",
    }

    # Reasoning-related keywords
    REASONING_KEYWORDS = {
        "explain",
        "why",
        "how",
        "analyze",
        "reason",
        "infer",
        "deduce",
        "conclude",
        "derive",
        "solve",
        "think",
        "understand",
        "interpret",
        "clarify",
        "elaborate",
        "compare",
        "contrast",
        "difference",
        "similarity",
        "logic",
        "argument",
        "evidence",
        "proof",
    }

    # Summarization-related keywords
    SUMMARIZATION_KEYWORDS = {
        "summarize",
        "summary",
        "tldr",
        "brief",
        "overview",
        "outline",
        "abstract",
        "condense",
        "digest",
        "recap",
        "highlight",
        "extract",
        "key",
        "main",
        "point",
        "synopsis",
        "gist",
    }

    # Quick question patterns (low complexity)
    QUICK_PATTERNS = {
        "what is",
        "what's",
        "who is",
        "who's",
        "when is",
        "where is",
        "how many",
        "how much",
        "why",
        "define",
        "meaning",
    }

    def classify(self, prompt: str, context: dict[str, Any] | None = None) -> TaskProfile:
        """Classify a prompt into a task profile.

        Args:
            prompt: The user prompt/question
            context: Optional context dict with keys like:
                - code: Code snippets present
                - retrieved_context: Retrieved knowledge base content
                - context_tokens: Estimated context token count

        Returns:
            TaskProfile with task type, complexity, and routing hints
        """
        context = context or {}

        # Estimate prompt length in tokens (rough: 1 token ~= 4 chars)
        prompt_tokens = max(1, len(prompt) // 4)
        context_tokens = context.get("context_tokens", 0)
        total_tokens = prompt_tokens + context_tokens

        # Classify task type
        task_type = self._classify_task_type(prompt)

        # Estimate complexity
        complexity = self._estimate_complexity(prompt, task_type, total_tokens, context)

        # Determine if latency sensitive
        latency_sensitive = self._is_latency_sensitive(prompt, task_type, complexity)

        # Check if long context needed
        requires_long_context = total_tokens > 8000

        # Check if tools needed
        requires_tools = self._requires_tools(prompt, context)

        return TaskProfile(
            task_type=task_type,
            complexity=complexity,
            latency_sensitive=latency_sensitive,
            requires_long_context=requires_long_context,
            requires_tools=requires_tools,
            estimated_tokens=total_tokens,
            metadata={
                "prompt_tokens": prompt_tokens,
                "context_tokens": context_tokens,
            },
        )

    def _classify_task_type(self, prompt: str) -> TaskType:
        """Classify the primary task type from prompt keywords."""
        lower = prompt.lower()

        # Check coding keywords
        coding_matches = sum(1 for kw in self.CODING_KEYWORDS if kw in lower)
        reasoning_matches = sum(1 for kw in self.REASONING_KEYWORDS if kw in lower)
        summarization_matches = sum(1 for kw in self.SUMMARIZATION_KEYWORDS if kw in lower)

        # Return task type with most matches
        max_matches = max(coding_matches, reasoning_matches, summarization_matches)

        if max_matches == 0:
            return TaskType.GENERAL

        if coding_matches == max_matches:
            return TaskType.CODING
        if reasoning_matches == max_matches:
            return TaskType.REASONING
        if summarization_matches == max_matches:
            return TaskType.SUMMARIZATION

        return TaskType.GENERAL

    def _estimate_complexity(
        self,
        prompt: str,
        task_type: TaskType,
        total_tokens: int,
        context: dict[str, Any],
    ) -> ComplexityLevel:
        """Estimate task complexity based on prompt and context."""
        prompt.lower()

        # Short prompts with no code/context => LOW
        if len(prompt) < 50 and total_tokens < 500:
            if task_type == TaskType.GENERAL and "code" not in context:
                return ComplexityLevel.LOW

        # Long prompts with code/context => HIGH
        if total_tokens > 4000 and "code" in context:
            return ComplexityLevel.HIGH

        # Reasoning/complex analysis => at least MEDIUM
        if task_type in (TaskType.REASONING, TaskType.SUMMARIZATION):
            if total_tokens > 2000:
                return ComplexityLevel.HIGH
            return ComplexityLevel.MEDIUM

        # Coding with substantial context => HIGH
        if task_type == TaskType.CODING and total_tokens > 3000:
            return ComplexityLevel.HIGH

        # Default to MEDIUM
        return ComplexityLevel.MEDIUM

    def _is_latency_sensitive(
        self,
        prompt: str,
        task_type: TaskType,
        complexity: ComplexityLevel,
    ) -> bool:
        """Determine if response latency is critical."""
        # Short quick questions are latency sensitive
        if complexity == ComplexityLevel.LOW:
            lower = prompt.lower()
            return any(pattern in lower for pattern in self.QUICK_PATTERNS)

        # Complex reasoning/analysis can wait
        if complexity == ComplexityLevel.HIGH:
            return False

        # Quick questions/definitions
        lower = prompt.lower()
        quick_match = any(pattern in lower for pattern in self.QUICK_PATTERNS)
        if quick_match:
            return True

        # Default: not latency sensitive for medium complexity
        return False

    def _requires_tools(self, prompt: str, context: dict[str, Any]) -> bool:
        """Determine if the task likely needs external tools/APIs."""
        tool_keywords = {
            "api",
            "request",
            "fetch",
            "call",
            "network",
            "http",
            "search",
            "browse",
            "lookup",
            "query",
            "database",
        }
        lower = prompt.lower()
        return any(kw in lower for kw in tool_keywords)
