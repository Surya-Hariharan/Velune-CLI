"""Query decomposition and expansion."""

from typing import list, Dict


class QueryAnalyzer:
    """Analyzes and decomposes queries."""

    def analyze(self, query: str) -> Dict[str, any]:
        """Analyze a query."""
        return {
            "original": query,
            "tokens": self._tokenize(query),
            "entities": self._extract_entities(query),
            "intent": self._detect_intent(query),
        }

    def _tokenize(self, query: str) -> list[str]:
        """Tokenize query."""
        return query.lower().split()

    def _extract_entities(self, query: str) -> list[str]:
        """Extract entities from query."""
        entities = []
        words = query.split()
        for word in words:
            if word[0].isupper() and len(word) > 1:
                entities.append(word)
        return entities

    def _detect_intent(self, query: str) -> str:
        """Detect query intent."""
        query_lower = query.lower()
        
        if any(word in query_lower for word in ["how", "what", "why", "explain"]):
            return "explanation"
        elif any(word in query_lower for word in ["find", "search", "where", "locate"]):
            return "search"
        elif any(word in query_lower for word in ["fix", "debug", "error"]):
            return "debugging"
        else:
            return "general"
