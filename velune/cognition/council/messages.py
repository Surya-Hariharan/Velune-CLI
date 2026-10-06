from typing import Any, Literal

from pydantic import BaseModel, Field

AgentStatus = Literal["ok", "unavailable", "unparseable"]


class _AgentMessage(BaseModel):
    """Common shape of every typed council message.

    ``status`` says whether the seat delivered a usable verdict. A seat that
    timed out, errored, or returned something unparseable *abstains*: consumers
    must check ``usable`` before reading ``passed``/``score``, and the
    degraded constructors below never look like an approval.
    """

    parse_error: str | None = None  # Set if the seat failed or its JSON was unparseable
    status: AgentStatus = "ok"

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    @classmethod
    def _abstain_defaults(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def degraded(cls, status: AgentStatus, reason: str):
        """A message standing in for a seat that did not deliver a verdict."""
        return cls.model_construct(status=status, parse_error=reason, **cls._abstain_defaults())

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


class ReviewerMessage(_AgentMessage):
    passed: bool = True
    critical_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    confidence_rating: float = 0.5

    @classmethod
    def _abstain_defaults(cls) -> dict[str, Any]:
        return {"passed": False, "confidence_rating": 0.0}


class ChallengerMessage(_AgentMessage):
    assumptions_challenged: list[str] = Field(default_factory=list)
    failure_vectors: list[str] = Field(default_factory=list)
    severity_rating: float = 0.0


class CriticMessage(_AgentMessage):
    passed: bool = True
    issues: list[str] = Field(default_factory=list)
    score: float = 0.9
    rationale: str = ""

    @classmethod
    def _abstain_defaults(cls) -> dict[str, Any]:
        return {"passed": False, "score": 0.0}


class PlannerMessage(_AgentMessage):
    task_id: str = "task-main"
    steps: list[dict[str, Any]] = Field(default_factory=list)
