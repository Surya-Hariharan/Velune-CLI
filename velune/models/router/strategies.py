"""Routing strategies."""

from enum import Enum


class RoutingStrategy(str, Enum):
    """Routing strategy options."""
    QUALITY_FIRST = "quality_first"  # Maximize capability match, ignore speed and cost
    SPEED_FIRST = "speed_first"  # Prefer fastest models meeting minimum capability threshold
    COST_AWARE = "cost_aware"  # Minimize cost while meeting quality requirements (cloud models only)
    LOCAL_FIRST = "local_first"  # Prefer local models, fall back to cloud only when capability gap exists
