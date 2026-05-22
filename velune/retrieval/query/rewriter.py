"""Query rewriting pipeline."""

from typing import list


class QueryRewriter:
    """Rewrites queries for better retrieval."""

    def __init__(self):
        pass

    def expand(self, query: str) -> list[str]:
        """Expand query with synonyms and related terms."""
        # Simple expansion - in production, use thesaurus/wordnet
        expansions = [query]
        
        # Add common variations
        if "bug" in query.lower():
            expansions.append(query.replace("bug", "error"))
            expansions.append(query.replace("bug", "issue"))
        
        if "fix" in query.lower():
            expansions.append(query.replace("fix", "resolve"))
            expansions.append(query.replace("fix", "solve"))
        
        return list(set(expansions))

    def simplify(self, query: str) -> str:
        """Simplify query by removing stop words."""
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being"}
        words = query.split()
        simplified = [w for w in words if w.lower() not in stop_words]
        return " ".join(simplified)
