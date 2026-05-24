from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class ReviewerMessage(BaseModel):
    passed: bool = True
    critical_issues: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    confidence_rating: float = 0.5
    parse_error: Optional[str] = None  # Set if JSON parsing failed

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

class ChallengerMessage(BaseModel):
    assumptions_challenged: List[str] = Field(default_factory=list)
    failure_vectors: List[str] = Field(default_factory=list)
    severity_rating: float = 0.0
    parse_error: Optional[str] = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

class CriticMessage(BaseModel):
    passed: bool = True
    issues: List[str] = Field(default_factory=list)
    score: float = 0.9
    rationale: str = ""
    parse_error: Optional[str] = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

class PlannerMessage(BaseModel):
    task_id: str = "task-main"
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    parse_error: Optional[str] = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)
