from typing import Any

from pydantic import BaseModel, Field


class ReviewerMessage(BaseModel):
    passed: bool = True
    critical_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    confidence_rating: float = 0.5
    parse_error: str | None = None  # Set if JSON parsing failed

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

class ChallengerMessage(BaseModel):
    assumptions_challenged: list[str] = Field(default_factory=list)
    failure_vectors: list[str] = Field(default_factory=list)
    severity_rating: float = 0.0
    parse_error: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

class CriticMessage(BaseModel):
    passed: bool = True
    issues: list[str] = Field(default_factory=list)
    score: float = 0.9
    rationale: str = ""
    parse_error: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

class PlannerMessage(BaseModel):
    task_id: str = "task-main"
    steps: list[dict[str, Any]] = Field(default_factory=list)
    parse_error: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)
