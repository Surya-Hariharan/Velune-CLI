"""Repository health signals."""

from typing import Dict, list
from velune.repository.cognition.model import RepositoryCognitiveModel


class RepositoryHealthAnalyzer:
    """Analyzes repository health signals."""

    def __init__(self, model: RepositoryCognitiveModel):
        self.model = model

    def analyze(self) -> Dict[str, any]:
        """Analyze repository health."""
        stats = self.model.get_statistics()
        
        health = {
            "overall_score": 0.0,
            "signals": {},
        }
        
        # File count signal
        file_count = stats["file_count"]
        if file_count > 0:
            health["signals"]["file_count"] = "healthy"
        else:
            health["signals"]["file_count"] = "empty"
        
        # Symbol density signal
        if file_count > 0:
            symbol_density = stats["symbol_count"] / file_count
            if symbol_density > 5:
                health["signals"]["symbol_density"] = "high"
            elif symbol_density > 1:
                health["signals"]["symbol_density"] = "normal"
            else:
                health["signals"]["symbol_density"] = "low"
        
        # Calculate overall score
        score = 0.5  # Base score
        if health["signals"].get("file_count") == "healthy":
            score += 0.3
        if health["signals"].get("symbol_density") == "normal":
            score += 0.2
        
        health["overall_score"] = min(score, 1.0)
        
        return health
