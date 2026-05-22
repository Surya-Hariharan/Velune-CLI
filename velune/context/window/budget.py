"""Token budget allocation per tier."""

from typing import Dict


class TokenBudget:
    """Manages token budget allocation across priority tiers."""

    def __init__(self, max_tokens: int = 128000):
        self.max_tokens = max_tokens
        # Default allocation percentages
        self.default_allocation = {
            "critical": 0.30,  # 30% for critical
            "high": 0.25,      # 25% for high
            "medium": 0.25,    # 25% for medium
            "low": 0.20,       # 20% for low
        }

    def allocate(self, custom_allocation: Optional[Dict[str, float]] = None) -> Dict[str, int]:
        """Allocate token budget across priority tiers."""
        allocation = custom_allocation or self.default_allocation
        
        budget = {}
        for tier, percentage in allocation.items():
            budget[tier] = int(self.max_tokens * percentage)
        
        return budget

    def get_remaining(self, used: int) -> int:
        """Get remaining tokens."""
        return max(0, self.max_tokens - used)

    def set_allocation(self, tier: str, percentage: float) -> None:
        """Set allocation percentage for a tier."""
        if 0 <= percentage <= 1:
            self.default_allocation[tier] = percentage
