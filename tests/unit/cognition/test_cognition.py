import pytest
from velune.cognition.arbitrator import CouncilArbitrator, ArbitrationResult

def test_calibrated_confidence():
    arbitrator = CouncilArbitrator(historical_accuracy=0.8)
    
    # Let's calculate calibrated confidence manually
    # S = self_reported * 0.2 + agreement_rate * 0.4 + historical_accuracy * 0.25 + logic_score * 0.15
    # S = 0.8 * 0.2 + 0.9 * 0.4 + 0.8 * 0.25 + 0.85 * 0.15 = 0.16 + 0.36 + 0.20 + 0.1275 = 0.8475 -> round to 0.848
    score = arbitrator.calculate_calibrated_confidence(
        self_reported=0.8,
        agreement_rate=0.9,
        logic_score=0.85
    )
    assert abs(score - 0.847) < 0.001

def test_arbitrate_success_flow():
    arbitrator = CouncilArbitrator(historical_accuracy=0.85)
    
    plan_steps = ["1. Modify file_a", "2. Run tests"]
    coder_proposal = "def add(x, y): return x + y"
    
    reviewer_report = {
        "passed": True,
        "confidence_rating": 0.9,
        "critical_issues": []
    }
    
    challenger_report = {
        "failure_vectors": [],
        "severity_rating": 0.1
    }
    
    result = arbitrator.arbitrate(
        plan_steps=plan_steps,
        coder_proposal=coder_proposal,
        reviewer_report=reviewer_report,
        challenger_report=challenger_report
    )
    
    assert not result.requires_human_review
    assert result.overall_confidence > 0.8
    assert "Coder solution is syntactically sound and logical." in result.winning_claims
    assert "fully approved" in result.synthesis_instructions

def test_arbitrate_critical_failures_flow():
    arbitrator = CouncilArbitrator(historical_accuracy=0.85)
    
    plan_steps = ["1. Modify file_a", "2. Run tests"]
    coder_proposal = "def bug_func(x): return 1 / x"  # Div by zero vulnerability
    
    reviewer_report = {
        "passed": False,
        "confidence_rating": 0.8,
        "critical_issues": ["Vulnerability: division by zero when x=0"]
    }
    
    challenger_report = {
        "failure_vectors": ["Division by zero on empty input"],
        "severity_rating": 0.8
    }
    
    result = arbitrator.arbitrate(
        plan_steps=plan_steps,
        coder_proposal=coder_proposal,
        reviewer_report=reviewer_report,
        challenger_report=challenger_report
    )
    
    assert result.requires_human_review  # Should be flagged for human review!
    assert "CRITICAL_BUGS_DETECTED" in result.flags
    assert "division by zero" in result.synthesis_instructions
    assert "Division by zero on empty input" in result.synthesis_instructions
