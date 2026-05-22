"""Fact extraction from experiences."""

from typing import list
from velune.core.types import MemoryRecord, MemoryType


class FactExtractor:
    """Extracts facts from experiences for semantic memory."""

    def __init__(self):
        pass

    def extract_from_conversation(
        self,
        conversation: list[dict],
    ) -> list[str]:
        """Extract facts from a conversation."""
        facts = []
        
        for turn in conversation:
            content = turn.get("content", "")
            # Simple fact extraction - in production, use LLM
            if "is" in content or "are" in content:
                facts.append(content)
        
        return facts

    def extract_from_code(
        self,
        code: str,
        file_path: str,
    ) -> list[str]:
        """Extract facts from code."""
        facts = []
        
        # Extract function definitions
        lines = code.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("def ") or line.startswith("class "):
                facts.append(f"{file_path}: {line}")
        
        return facts

    def extract_from_documentation(
        self,
        documentation: str,
    ) -> list[str]:
        """Extract facts from documentation."""
        facts = []
        
        # Extract sentences that appear factual
        sentences = documentation.split(".")
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) > 20 and any(keyword in sentence.lower() for keyword in ["is", "are", "can", "will", "should"]):
                facts.append(sentence)
        
        return facts
