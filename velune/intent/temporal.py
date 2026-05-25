"""Temporal reference resolver."""

from __future__ import annotations

import re
import time


class TemporalResolver:
    """Resolves relative temporal references in user queries into absolute timestamps/offsets."""

    def __init__(self) -> None:
        self.rules = {
            r"\bjust now\b": 60,
            r"\b(an hour|1 hour) ago\b": 3600,
            r"\b(\d+) hours? ago\b": lambda m: int(m.group(1)) * 3600,
            r"\byesterday\b": 86400,
            r"\b(\d+) days? ago\b": lambda m: int(m.group(1)) * 86400,
            r"\blast week\b": 604800,
        }

    def resolve(self, text: str) -> float | None:
        """
        Scans text for temporal references and returns a relative time offset in seconds.
        Returns None if no temporal signals are present.
        """
        for pattern, rule in self.rules.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                if callable(rule):
                    seconds = rule(match)
                else:
                    seconds = rule
                return seconds
        return None

    def get_query_window(self, text: str) -> float | None:
        """
        Returns the absolute timestamp for the start of the query window.
        e.g., if query says "yesterday", returns current_time - 86400.
        """
        offset = self.resolve(text)
        if offset is not None:
            return time.time() - offset
        return None
