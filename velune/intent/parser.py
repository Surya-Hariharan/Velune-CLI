"""Intent Signal Parser.

Lexical signal parser that extracts file names, paths, verbs, and keywords
from raw user commands.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set


class IntentSignalParser:
    """Extracts linguistic and path-based signals from raw inputs."""

    def __init__(self) -> None:
        self.verb_patterns = [
            r"\b(create|make|build|generate|add)\b",
            r"\b(fix|debug|resolve|repair|patch)\b",
            r"\b(refactor|clean|optimize|improve)\b",
            r"\b(analyze|inspect|read|explain|review)\b",
        ]

    def parse(self, text: str) -> Dict[str, Any]:
        """Parse raw query and extract structured signal features."""
        # 1. Extract file paths / patterns
        # Matches patterns like src/main.py, test.js, README.md, etc.
        file_matches = re.findall(r"[\w\-\./]+\.[a-zA-Z]{2,4}", text)
        
        # 2. Extract action verbs
        actions: Set[str] = set()
        for pattern in self.verb_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for m in matches:
                actions.add(m.lower())

        # 3. Extract directories
        dir_matches = re.findall(r"[\w\-]+/+(?:[\w\-]+/)*", text)

        # 4. Extract possible commands or script names
        code_blocks = re.findall(r"`([^`]+)`", text)

        return {
            "raw_text": text,
            "target_files": list(set(file_matches)),
            "target_directories": list(set(dir_matches)),
            "action_verbs": list(actions),
            "code_snippets": code_blocks,
        }
