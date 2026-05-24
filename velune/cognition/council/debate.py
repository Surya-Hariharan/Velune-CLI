from dataclasses import dataclass
from typing import List

@dataclass
class DebateConfig:
    """Controls Council debate dynamics."""
    max_turns: int = 3
    min_turns: int = 1
    convergence_threshold: float = 0.0  # Stop if 0 objections remain
    severity_turn_boost: float = 0.8    # If avg severity > this, add extra turn
    critical_issue_hard_stop: bool = True  # Stop if reviewer flags critical security issue

def calculate_max_debate_turns(
    initial_objections: List[str],
    critic_reports: dict,
    task_complexity: str,  # "simple" | "structural"
    base_max: int = 3,
) -> int:
    """
    Dynamically calculate max debate turns based on:
    - Number and severity of objections
    - Task structural complexity
    - Specific critic failure patterns
    """
    if not initial_objections:
        return 0  # No debate needed
    
    turns = base_max
    
    # Security objections always get extra turn
    security_failed = not critic_reports.get("security", {}).get("passed", True)
    if security_failed:
        turns = max(turns, 4)
    
    # High challenger severity adds turn
    challenger_severity = critic_reports.get("challenger", {}).get("severity_rating", 0.0)
    if challenger_severity > 0.8:
        turns += 1
    
    # Simple tasks cap lower
    if task_complexity == "simple":
        turns = min(turns, 2)
    
    return min(turns, 5)  # Hard cap at 5
