"""Data passed between the router, the stage loop, agents and the run store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: AgentResult.status values. Only "ok" counts as an answer.
STATUSES = ("ok", "error", "timeout", "denied", "budget", "cancelled")


@dataclass
class AgentSpec:
    """One agent to create: a role from the skill, with its own sub-task."""

    id: str  # unique in the run; equals role unless a role is used twice
    role: str  # key of the "## role" section in SKILL.md
    task: str  # the specific sub-task the router assigned
    stage: int = 0  # position in the flow
    can_write: bool = False  # role is a skill writer AND the profile allows writes
    can_act: bool = False  # the one agent that may change things through integrations
    max_turns: int = 8


@dataclass
class SwarmPlan:
    """What will run; shown to the user before anything starts."""

    task: str  # the user's request, verbatim
    skill: str
    permission: str
    output: str  # text | plan | diff | files | diagram
    lead: str  # id of the agent that writes the final output
    agents: list[AgentSpec]
    stages: list[list[str]]  # agent ids per stage
    notes: list[str] = field(default_factory=list)  # downgrades, splits, fallbacks

    def agent(self, agent_id: str) -> AgentSpec:
        return next(a for a in self.agents if a.id == agent_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    """What one agent hands back."""

    agent_id: str
    status: str
    content: str = ""
    summary: str = ""  # short form used in later agents' prompts
    tool_calls: list[str] = field(default_factory=list)  # "read_file(velune/x.py)"
    files_touched: list[str] = field(default_factory=list)
    tokens: int = 0
    elapsed_s: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
