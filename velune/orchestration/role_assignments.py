"""Council role assignment data model — persisted as JSON under ~/.velune/."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

COUNCIL_ROLES: list[str] = [
    "planner",
    "coder",
    "reviewer",
    "architect",
    "security",
    "challenger",
    "synthesizer",
    "embedding",
]

ROLE_DESCRIPTIONS: dict[str, str] = {
    "planner": "Decomposes tasks, retrieves context, plans execution steps",
    "coder": "Writes code, generates patches, implements solutions",
    "reviewer": "Audits code quality, catches regressions, checks correctness",
    "architect": "Defines software architectures and system blueprints",
    "security": "Reviews security constraints and handles threat mitigation",
    "challenger": "Argues against proposals, finds edge cases, stress-tests logic",
    "synthesizer": "Combines agent outputs into a final coherent response",
    "embedding": "Generates vector embeddings for semantic memory and retrieval",
}


@dataclass
class RoleAssignment:
    role: str
    model_id: str
    provider_id: str


@dataclass
class CouncilRoleMap:
    assignments: dict[str, RoleAssignment] = field(default_factory=dict)

    def assign(self, role: str, model_id: str, provider_id: str) -> None:
        if role not in COUNCIL_ROLES:
            raise ValueError(f"Unknown role: {role!r}. Valid roles: {COUNCIL_ROLES}")
        self.assignments[role] = RoleAssignment(role, model_id, provider_id)

    def get(self, role: str) -> RoleAssignment | None:
        return self.assignments.get(role)

    def clear_role(self, role: str) -> None:
        self.assignments.pop(role, None)

    def clear_all(self) -> None:
        self.assignments.clear()

    def to_dict(self) -> dict:
        return {
            role: {"model_id": a.model_id, "provider_id": a.provider_id}
            for role, a in self.assignments.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> CouncilRoleMap:
        role_map = cls()
        for role, info in data.items():
            role_map.assign(role, info["model_id"], info["provider_id"])
        return role_map

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), indent=2)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(str(tmp), str(path))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    @classmethod
    def load(cls, path: Path) -> CouncilRoleMap:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except Exception:
            return cls()
